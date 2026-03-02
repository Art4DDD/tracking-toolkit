import ctypes
import datetime
import queue
import threading
from typing import Generator

import bpy
import bpy_extras
import openvr
from mathutils import Matrix, Quaternion

from .properties import OVRContext, OVRTracker, OVRInput

# Shared variables

pose_queue = queue.Queue()
polling_thread = None
stop_thread_flag = threading.Event()

preview_buffer = []
record_buffer = []
buffer_lock = threading.Lock()
recording_active = False

action_sets = []
action_handles = {}

FINGER_BONE_CHAINS = {
    "thumb": (2, 3, 4, 5),
    "index": (6, 7, 8, 9, 10),
    "middle": (11, 12, 13, 14, 15),
    "ring": (16, 17, 18, 19, 20),
    "pinky": (21, 22, 23, 24, 25),
}


def init_handles():
    vr_ipt = openvr.VRInput()

    def _get_action_set_handle(action_set_path: str):
        try:
            return vr_ipt.getActionSetHandle(action_set_path)
        except Exception:
            return None

    def _get_action_handle(action_path: str):
        try:
            return vr_ipt.getActionHandle(action_path)
        except Exception:
            return None

    global action_sets
    action_set_handle = _get_action_set_handle("/actions/default")
    action_sets = (openvr.VRActiveActionSet_t * 1)()
    action_sets[0].ulActionSet = action_set_handle or 0

    global action_handles
    action_handles = {
        "l_skeleton": _get_action_handle("/actions/default/in/SkeletonLeftHand"),
        "r_skeleton": _get_action_handle("/actions/default/in/SkeletonRightHand"),
    }

    print("Initialized OpenVR skeletal action handles")


def _handle_input(ovr_context: OVRContext):
    l_ipt = ovr_context.l_input
    r_ipt = ovr_context.r_input


def _get_input(ovr_context: OVRContext):
    if not (action_handles and action_sets):
        return

    vr_ipt = openvr.VRInput()
    l_ipt: OVRInput = ovr_context.l_input
    r_ipt: OVRInput = ovr_context.r_input

    # Match C++ flow: update action state with explicit element size/count when available.
    try:
        vr_ipt.updateActionState(action_sets, ctypes.sizeof(openvr.VRActiveActionSet_t), len(action_sets))
    except TypeError:
        try:
            vr_ipt.updateActionState(action_sets)
        except Exception:
            return
    except Exception:
        return

    def _calc_finger_curl(bone_transforms, chain: tuple[int, ...]) -> float:
        if len(chain) < 2:
            return 0.0

        curls = []
        for i in range(len(chain) - 1):
            parent_idx = chain[i]
            child_idx = chain[i + 1]
            parent = bone_transforms[parent_idx]
            child = bone_transforms[child_idx]

            pv = parent.position.v
            cv = child.position.v
            vec = (cv[0] - pv[0], cv[1] - pv[1], cv[2] - pv[2])
            length = (vec[0] ** 2 + vec[1] ** 2 + vec[2] ** 2) ** 0.5
            if length <= 1e-6:
                continue

            forward = vec[1] / length
            curl = max(0.0, min(1.0, (1.0 - forward) * 0.5))
            curls.append(curl)

        if not curls:
            return 0.0

        return sum(curls) / len(curls)

    def _get_skeletal_finger_curls(action_key: str) -> dict[str, float] | None:
        action = action_handles.get(action_key)
        if action is None:
            return None

        def _resolve_method(names: tuple[str, ...]):
            for name in names:
                method = getattr(vr_ipt, name, None)
                if method:
                    return method
            return None

        get_action_data_fn = _resolve_method(("getSkeletalActionData", "GetSkeletalActionData"))
        if not get_action_data_fn:
            return None

        action_data = None
        try:
            action_data = get_action_data_fn(action)
        except TypeError:
            action_data_t = getattr(openvr, "InputSkeletalActionData_t", None)
            if action_data_t:
                try:
                    action_data = action_data_t()
                    get_action_data_fn(action, action_data, ctypes.sizeof(action_data_t))
                except Exception:
                    action_data = None
        except Exception:
            action_data = None

        if action_data is None or not getattr(action_data, "bActive", False):
            return None

        # 1) Preferred path: direct summary curls.
        get_summary_fn = _resolve_method(("getSkeletalSummaryData", "GetSkeletalSummaryData"))
        if get_summary_fn:
            summary = None
            try:
                summary = get_summary_fn(action)
            except TypeError:
                summary_data_t = getattr(openvr, "InputSkeletalSummaryData_t", None)
                if summary_data_t:
                    try:
                        summary = summary_data_t()
                        get_summary_fn(action, summary, ctypes.sizeof(summary_data_t))
                    except Exception:
                        summary = None
            except Exception:
                summary = None

            if summary is not None:
                curls = getattr(summary, "flFingerCurl", None) or getattr(summary, "fingerCurl", None)
                if curls and len(curls) >= 5:
                    return {
                        "thumb": float(curls[0]),
                        "index": float(curls[1]),
                        "middle": float(curls[2]),
                        "ring": float(curls[3]),
                        "pinky": float(curls[4]),
                    }

        # 2) Fallback path: estimate from bone data.
        get_bone_data_fn = _resolve_method(("getSkeletalBoneData", "GetSkeletalBoneData"))
        if not get_bone_data_fn:
            return None

        transform_space = getattr(openvr, "VRSkeletalTransformSpace_Model", 0)
        motion_range = getattr(openvr, "VRSkeletalMotionRange_WithController", 0)
        bone_count = getattr(openvr, "k_unSkeletonBoneCount", 31)

        bone_transforms = None
        try:
            bone_transforms = get_bone_data_fn(action, transform_space, motion_range)
        except TypeError:
            bone_t = getattr(openvr, "VRBoneTransform_t", None)
            if bone_t:
                try:
                    buffer = (bone_t * bone_count)()
                    get_bone_data_fn(action, transform_space, motion_range, buffer, bone_count)
                    bone_transforms = list(buffer)
                except Exception:
                    bone_transforms = None
        except Exception:
            bone_transforms = None

        if not bone_transforms:
            return None

        result = {}
        for finger_name, chain in FINGER_BONE_CHAINS.items():
            valid_chain = tuple(idx for idx in chain if idx < len(bone_transforms))
            result[finger_name] = _calc_finger_curl(bone_transforms, valid_chain)

        return result


    l_skeletal = _get_skeletal_finger_curls("l_skeleton")
    if l_skeletal:
        l_ipt.thumb_curl = l_skeletal["thumb"]
        l_ipt.index_curl = l_skeletal["index"]
        l_ipt.middle_curl = l_skeletal["middle"]
        l_ipt.ring_curl = l_skeletal["ring"]
        l_ipt.pinky_curl = l_skeletal["pinky"]

    r_skeletal = _get_skeletal_finger_curls("r_skeleton")
    if r_skeletal:
        r_ipt.thumb_curl = r_skeletal["thumb"]
        r_ipt.index_curl = r_skeletal["index"]
        r_ipt.middle_curl = r_skeletal["middle"]
        r_ipt.ring_curl = r_skeletal["ring"]
        r_ipt.pinky_curl = r_skeletal["pinky"]


def _get_poses(ovr_context: OVRContext) -> Generator[tuple[datetime.datetime, OVRTracker, Matrix], None, None]:
    system = openvr.VRSystem()
    poses, _ = openvr.VRCompositor().waitGetPoses([], None)
    time = datetime.datetime.now()

    for tracker in ovr_context.trackers:
        if not bool(system.isTrackedDeviceConnected(tracker.index)):
            continue

        absolute_pose = poses[tracker.index].mDeviceToAbsoluteTracking

        mat = Matrix([list(absolute_pose[0]), list(absolute_pose[1]), list(absolute_pose[2]), [0, 0, 0, 1]])
        mat_world = bpy_extras.io_utils.axis_conversion("Z", "Y", "Y", "Z").to_4x4()
        mat_world = mat_world @ mat

        # Apply scale (use axis scale value directly; length of (1,1,1) is 1.732 and inflates transforms)
        root = bpy.data.objects.get("OVR Root")
        if root:
            mat_world = mat_world @ Matrix.Scale(root.scale.x, 4)

        yield time, tracker, mat_world


def _openvr_poll_thread_func(ovr_context: OVRContext):
    global pose_queue, stop_thread_flag, preview_buffer, record_buffer, buffer_lock, recording_active

    while not stop_thread_flag.is_set():
        pose_chunk = []
        for pose_data in _get_poses(ovr_context):
            pose_chunk.append(pose_data)

        with buffer_lock:
            preview_buffer.append(pose_chunk)
            if len(preview_buffer) > 2:
                preview_buffer.pop(0)

            if recording_active:
                record_buffer.append(pose_chunk)

        _get_input(ovr_context)
        _handle_input(ovr_context)


def _clear_buffer():
    global record_buffer, buffer_lock
    with buffer_lock:
        record_buffer.clear()


def _get_buffer() -> list[list[tuple[datetime.datetime, OVRTracker, Matrix]]]:
    global record_buffer, buffer_lock

    with buffer_lock:
        buffer_copy = record_buffer.copy()

    return buffer_copy


def _get_latest_poses() -> list[tuple[datetime.datetime, OVRTracker, Matrix]] | None:
    global preview_buffer, buffer_lock
    with buffer_lock:
        if len(preview_buffer) == 0:
            return None

        return preview_buffer[-1]


def _apply_poses():
    # Don't preview when playing, since a previous recording may interfere
    if bpy.context.screen.is_animation_playing:
        return

    pose_data = _get_latest_poses()
    if not pose_data:
        return

    for time, tracker, pose in pose_data:
        tracker_obj = bpy.data.objects.get(tracker.name)
        if not tracker_obj:
            continue

        tracker_obj.matrix_world = pose


def _pose_vis_timer():
    _apply_poses()
    return 1.0 / 60  # 60hz


def _insert_action(ovr_context: OVRContext):
    pose_data = _get_buffer()
    num_samples = len(pose_data)
    print(f"OpenVR Processing {num_samples} recorded samples")
    if num_samples == 0:
        print(f"OpenVR Found no samples to process")
        return

    take_start_time = pose_data[0][0][0]
    framerate = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    start_frame = ovr_context.record_start_frame

    animation_data = {}

    for sample in pose_data:
        for time, tracker, pose in sample:
            tracker_obj = bpy.data.objects.get(tracker.name)
            if not tracker_obj:
                continue

            if tracker_obj.animation_data is None:
                tracker_obj.animation_data_create()

            if tracker.name not in animation_data:
                animation_data[tracker.name] = {
                    "obj": tracker_obj,
                    "frames": [],
                    "locs": [],
                    "rots": []
                }

            time_delta = time - take_start_time
            frame = start_frame + time_delta.total_seconds() * framerate

            # получаем лок и рот из матрицы (scale не используем для производительности)
            loc, rot, _ = pose.decompose()

            # -----------------------------
            # СТАБИЛИЗАЦИЯ QUATERNION
            # -----------------------------
            data = animation_data[tracker.name]

            prev_quat = None
            if data["rots"]:
                # берем последний записанный quaternion (Blender хранит как [w, x, y, z])
                last_index = len(data["rots"]) - 4
                prev_quat_list = data["rots"][last_index:last_index+4]
                prev_quat = Quaternion(prev_quat_list)
                if prev_quat.dot(rot) < 0:
                    rot = -rot
            # -----------------------------

            # сохраняем ключевые данные
            data["frames"].append(frame)
            data["locs"].extend(loc)
            data["rots"].extend(rot)


    # Now insert or replace the data
    print("OpenVR Inserting data...")
    for tracker_name, data in animation_data.items():
        print(">", tracker_name)

        tracker_obj = data["obj"]
        num_keys = len(data["frames"])

        # Create animation data and action
        if not tracker_obj.animation_data:
            tracker_obj.animation_data_create()

        action = tracker_obj.animation_data.action
        if not action:
            action = bpy.data.actions.new(name=f"{tracker_obj.name}_Action")
            tracker_obj.animation_data.action = action

        # Map the F-Curve data_path and array_index to our collected data.
        fcurve_props = [
            ("location", 3, data["locs"]),
            ("rotation_quaternion", 4, data["rots"])
        ]

        for data_path, num_components, values in fcurve_props:
            for i in range(num_components):
                # Get or create the F-Curve
                fcurve = action.fcurves.find(data_path, index=i)
                if fcurve:
                    action.fcurves.remove(fcurve)
                fcurve = action.fcurves.new(data_path, index=i)

                # Fill with points
                fcurve.keyframe_points.add(num_keys)

                # Create the flattened list for foreach_set.
                # The format is [frame1, value1, frame2, value2, ...]

                # Initialize
                key_coords = [0.0] * (num_keys * 2)

                # We slice the values list to get the data for the current component (axis)
                component_values = values[i::num_components]

                key_coords[0::2] = data["frames"]
                key_coords[1::2] = component_values

                # Set all keyframe coordinates at once
                fcurve.keyframe_points.foreach_set("co", key_coords)

                # Update the fcurve to apply changes
                fcurve.update()

        # Select first action slot
        # Otherwise, the new keyframes will not show
        action_slot = action.slots[0]
        tracker_obj.animation_data.action_slot = action_slot

    print("Done")


def start_recording():
    global recording_active

    _clear_buffer()
    recording_active = True

    print("OpenVR Recording Started")


def stop_recording(ovr_context: OVRContext | None):
    global recording_active

    recording_active = False
    stop_preview()
    _insert_action(ovr_context)
    _clear_buffer()
    start_preview(ovr_context)

    print("OpenVR Recording Stopped")


def start_preview(ovr_context: OVRContext):
    global polling_thread, stop_thread_flag

    if polling_thread and polling_thread.is_alive():
        stop_thread_flag.set()
        polling_thread.join()

    stop_thread_flag.clear()
    polling_thread = threading.Thread(target=lambda: _openvr_poll_thread_func(ovr_context))
    polling_thread.daemon = True  # Quit with Blender
    polling_thread.start()

    if not bpy.app.timers.is_registered(_pose_vis_timer):
        bpy.app.timers.register(_pose_vis_timer)

    print("OpenVR Preview Started")


def stop_preview():
    global polling_thread, stop_thread_flag, preview_buffer

    if bpy.app.timers.is_registered(_pose_vis_timer):
        bpy.app.timers.unregister(_pose_vis_timer)

    if polling_thread and polling_thread.is_alive():
        stop_thread_flag.set()
        polling_thread.join()

    with buffer_lock:
        preview_buffer.clear()

    polling_thread = None
    stop_thread_flag.clear()

    print("OpenVR Preview Stopped")


def load_trackers(ovr_context: OVRContext):
    print("OpenVR Loading Trackers")
    system = openvr.VRSystem()

    ovr_context.trackers.clear()

    for i in range(openvr.k_unMaxTrackedDeviceCount):
        if system.getTrackedDeviceClass(i) == openvr.TrackedDeviceClass_Invalid:
            continue

        tracker_serial = system.getStringTrackedDeviceProperty(i, openvr.Prop_SerialNumber_String)
        tracker = ovr_context.trackers.add()
        tracker.name = tracker_serial
        tracker.prev_name = tracker_serial
        tracker.serial = tracker_serial
        tracker.type = str(system.getTrackedDeviceClass(i))
        tracker.index = i
        tracker.connected = bool(system.isTrackedDeviceConnected(i))  # Just in case, do it for both
