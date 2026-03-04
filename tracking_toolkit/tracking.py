import datetime
import ctypes
import queue
import threading
from typing import Generator

import bpy
import bpy_extras
import openvr
from mathutils import Matrix, Quaternion

from .properties import OVRContext, OVRTracker

# Shared variables

pose_queue = queue.Queue()
polling_thread = None
stop_thread_flag = threading.Event()

preview_buffer = []
record_buffer = []
input_record_buffer = []
buffer_lock = threading.Lock()
recording_active = False
action_sets = []
action_handles = {}

FINGER_CHANNELS = ("thumb_curl", "index_curl", "middle_curl", "ring_curl", "pinky_curl")
TRIGGER_CHANNEL = "trigger_value"
latest_input_state = {
    "left": {channel: 0.0 for channel in (*FINGER_CHANNELS, TRIGGER_CHANNEL)},
    "right": {channel: 0.0 for channel in (*FINGER_CHANNELS, TRIGGER_CHANNEL)},
}

FINGER_BONE_CHAINS = {
    "thumb": (2, 3, 4, 5),
    "index": (6, 7, 8, 9, 10),
    "middle": (11, 12, 13, 14, 15),
    "ring": (16, 17, 18, 19, 20),
    "pinky": (21, 22, 23, 24, 25),
}



def _ensure_finger_properties(target_obj: bpy.types.Object):
    for channel in FINGER_CHANNELS:
        if channel not in target_obj:
            target_obj[channel] = 0.0


def _set_action_slot_if_supported(obj: bpy.types.Object, action: bpy.types.Action):
    slots = getattr(action, "slots", None)
    if not slots:
        return

    if obj.animation_data is None:
        obj.animation_data_create()

    try:
        obj.animation_data.action_slot = slots[0]
    except Exception:
        pass



def _safe_getattr_bool(obj, name: str, default=False):
    try:
        return bool(getattr(obj, name))
    except Exception:
        return default


def _get_or_create_root_object() -> bpy.types.Object:
    root_obj = bpy.data.objects.get("OVR Root")
    if root_obj:
        return root_obj

    root_obj = bpy.data.objects.new("OVR Root", None)
    root_obj.empty_display_type = "CUBE"
    root_obj.empty_display_size = 0.1
    bpy.context.scene.collection.objects.link(root_obj)
    return root_obj

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
        "l_trigger_click": _get_action_handle("/actions/default/in/TriggerClickLeft"),
        "r_trigger_click": _get_action_handle("/actions/default/in/TriggerClickRight"),
    }

    print("Initialized OpenVR skeletal action handles")


def _handle_input(ovr_context: OVRContext, input_state: dict[str, dict[str, float]]):
    root_obj = _get_or_create_root_object()

    left_state = input_state.get("left", {})
    right_state = input_state.get("right", {})

    ovr_context.l_input.thumb_curl = float(left_state.get("thumb_curl", 0.0))
    ovr_context.l_input.index_curl = float(left_state.get("index_curl", 0.0))
    ovr_context.l_input.middle_curl = float(left_state.get("middle_curl", 0.0))
    ovr_context.l_input.ring_curl = float(left_state.get("ring_curl", 0.0))
    ovr_context.l_input.pinky_curl = float(left_state.get("pinky_curl", 0.0))
    ovr_context.l_input.trigger_strength = float(left_state.get(TRIGGER_CHANNEL, 0.0))

    ovr_context.r_input.thumb_curl = float(right_state.get("thumb_curl", 0.0))
    ovr_context.r_input.index_curl = float(right_state.get("index_curl", 0.0))
    ovr_context.r_input.middle_curl = float(right_state.get("middle_curl", 0.0))
    ovr_context.r_input.ring_curl = float(right_state.get("ring_curl", 0.0))
    ovr_context.r_input.pinky_curl = float(right_state.get("pinky_curl", 0.0))
    ovr_context.r_input.trigger_strength = float(right_state.get(TRIGGER_CHANNEL, 0.0))

    channel_map = {
        "left_thumb_curl": ovr_context.l_input.thumb_curl,
        "left_index_curl": ovr_context.l_input.index_curl,
        "left_middle_curl": ovr_context.l_input.middle_curl,
        "left_ring_curl": ovr_context.l_input.ring_curl,
        "left_pinky_curl": ovr_context.l_input.pinky_curl,
        "right_thumb_curl": ovr_context.r_input.thumb_curl,
        "right_index_curl": ovr_context.r_input.index_curl,
        "right_middle_curl": ovr_context.r_input.middle_curl,
        "right_ring_curl": ovr_context.r_input.ring_curl,
        "right_pinky_curl": ovr_context.r_input.pinky_curl,
    }

    for channel, value in channel_map.items():
        if channel not in root_obj:
            root_obj[channel] = 0.0
        root_obj[channel] = value


def _controller_trigger_values(vr_ipt, ovr_context: OVRContext) -> tuple[float, float]:
    def _digital_state(action_key: str) -> tuple[bool, bool]:
        action = action_handles.get(action_key)
        if action is None:
            return False, False

        get_digital_fn = getattr(vr_ipt, "getDigitalActionData", None) or getattr(vr_ipt, "GetDigitalActionData", None)
        if not get_digital_fn:
            return False, False

        variants = [
            lambda: get_digital_fn(action),
            lambda: get_digital_fn(action, openvr.k_ulInvalidInputValueHandle),
        ]

        for call in variants:
            try:
                digital_data = call()
            except Exception:
                continue

            if isinstance(digital_data, tuple) and len(digital_data) >= 1:
                digital_data = digital_data[0]

            if not digital_data:
                continue

            active = _safe_getattr_bool(digital_data, "bActive", True)
            state = _safe_getattr_bool(digital_data, "bState", False)
            return active, state

        return False, False

    left_active, left_pressed = _digital_state("l_trigger_click")
    right_active, right_pressed = _digital_state("r_trigger_click")

    if not (left_active or right_active):
        try:
            system = openvr.VRSystem()
            left_role = getattr(openvr, "TrackedControllerRole_LeftHand", 1)
            right_role = getattr(openvr, "TrackedControllerRole_RightHand", 2)
            role_prop = getattr(openvr, "Prop_ControllerRoleHint_Int32", None)
            trigger_button = getattr(openvr, "k_EButton_SteamVR_Trigger", 33)
            trigger_mask = 1 << trigger_button

            for tracker in ovr_context.trackers:
                if tracker.type != str(openvr.TrackedDeviceClass_Controller):
                    continue

                role = None
                if role_prop is not None:
                    try:
                        role = system.getInt32TrackedDeviceProperty(tracker.index, role_prop)
                    except Exception:
                        role = None

                if role is None:
                    get_role_fn = getattr(system, "getControllerRoleForTrackedDeviceIndex", None) or getattr(system, "GetControllerRoleForTrackedDeviceIndex", None)
                    if get_role_fn:
                        try:
                            role = get_role_fn(tracker.index)
                        except Exception:
                            role = None

                try:
                    controller_state = system.getControllerState(tracker.index)
                except Exception:
                    continue

                if isinstance(controller_state, tuple):
                    controller_state = controller_state[1] if len(controller_state) >= 2 else controller_state[0]

                pressed = bool(getattr(controller_state, "ulButtonPressed", 0) & trigger_mask)
                if role == left_role:
                    left_active, left_pressed = True, pressed
                elif role == right_role:
                    right_active, right_pressed = True, pressed
        except Exception:
            pass

    return float(left_pressed), float(right_pressed)


def _get_input(ovr_context: OVRContext) -> dict[str, dict[str, float]] | None:
    vr_ipt = openvr.VRInput()

    updated = False
    if action_handles and action_sets:
        update_variants = [
            lambda: vr_ipt.updateActionState(action_sets, ctypes.sizeof(openvr.VRActiveActionSet_t), len(action_sets)),
            lambda: vr_ipt.updateActionState(action_sets),
            lambda: vr_ipt.updateActionState(action_sets[0], ctypes.sizeof(openvr.VRActiveActionSet_t), 1),
        ]

        for update_call in update_variants:
            try:
                update_call()
                updated = True
                break
            except Exception:
                continue

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

        get_action_data_fn = getattr(vr_ipt, "getSkeletalActionData", None) or getattr(vr_ipt, "GetSkeletalActionData", None)
        if not get_action_data_fn:
            return None

        try:
            action_data = get_action_data_fn(action)
        except Exception:
            return None

        action_active = _safe_getattr_bool(action_data, "bActive", False)

        # Some mixed-controller setups report inactive action data even when summary/bone data is readable.
        # Do not hard-stop on bActive here; try data retrieval paths below.

        # 1) Preferred path: skeletal summary already contains per-finger curls.
        get_summary_fn = getattr(vr_ipt, "getSkeletalSummaryData", None) or getattr(vr_ipt, "GetSkeletalSummaryData", None)
        summary = None
        if get_summary_fn:
            summary_data_t = getattr(openvr, "InputSkeletalSummaryData_t", None)
            summary_type = getattr(openvr, "VRSummaryType_FromDevice", 1)

            summary_variants = [
                lambda: get_summary_fn(action),
                lambda: get_summary_fn(action, summary_type),
            ]
            if summary_data_t:
                summary_variants.extend([
                    lambda: get_summary_fn(action, summary_data_t()),
                    lambda: get_summary_fn(action, summary_type, summary_data_t()),
                ])

            for summary_call in summary_variants:
                try:
                    summary = summary_call()
                    break
                except Exception:
                    continue

        if isinstance(summary, tuple) and len(summary) >= 1:
            summary = summary[0]

        if summary is not None:
            curls = getattr(summary, "flFingerCurl", None) or getattr(summary, "fingerCurl", None)
            if curls and len(curls) >= 5:
                result = {
                    "thumb": float(curls[0]),
                    "index": float(curls[1]),
                    "middle": float(curls[2]),
                    "ring": float(curls[3]),
                    "pinky": float(curls[4]),
                }
                if (not action_active) and max(result.values()) <= 1e-4:
                    return None
                return result

        # 2) Fallback path: estimate curls from skeletal bone transforms.
        get_bone_data_fn = getattr(vr_ipt, "getSkeletalBoneData", None) or getattr(vr_ipt, "GetSkeletalBoneData", None)
        if not get_bone_data_fn:
            return None

        transform_space = getattr(openvr, "VRSkeletalTransformSpace_Model", 0)
        motion_range = getattr(openvr, "VRSkeletalMotionRange_WithController", 0)
        bone_count = getattr(openvr, "k_unSkeletonBoneCount", 31)

        bone_transforms = None
        bone_variants = [
            lambda: get_bone_data_fn(action, transform_space, motion_range),
            lambda: get_bone_data_fn(action, transform_space, motion_range, bone_count),
            lambda: get_bone_data_fn(action, transform_space, motion_range, None, bone_count),
        ]

        for bone_call in bone_variants:
            try:
                bone_transforms = bone_call()
                break
            except Exception:
                continue

        if isinstance(bone_transforms, tuple) and len(bone_transforms) >= 1:
            bone_transforms = bone_transforms[0]

        if not bone_transforms:
            return None

        result = {}
        for finger_name, chain in FINGER_BONE_CHAINS.items():
            valid_chain = tuple(idx for idx in chain if idx < len(bone_transforms))
            result[finger_name] = _calc_finger_curl(bone_transforms, valid_chain)

        if (not action_active) and max(result.values()) <= 1e-4:
            return None
        return result

    l_skeletal = _get_skeletal_finger_curls("l_skeleton") if updated else None
    r_skeletal = _get_skeletal_finger_curls("r_skeleton") if updated else None

    with buffer_lock:
        previous_left = latest_input_state["left"].copy()
        previous_right = latest_input_state["right"].copy()

    left_trigger, right_trigger = _controller_trigger_values(vr_ipt, ovr_context)

    left_data = {
        "thumb_curl": float((l_skeletal or {}).get("thumb", previous_left["thumb_curl"])),
        "index_curl": float((l_skeletal or {}).get("index", previous_left["index_curl"])),
        "middle_curl": float((l_skeletal or {}).get("middle", previous_left["middle_curl"])),
        "ring_curl": float((l_skeletal or {}).get("ring", previous_left["ring_curl"])),
        "pinky_curl": float((l_skeletal or {}).get("pinky", previous_left["pinky_curl"])),
        TRIGGER_CHANNEL: float(left_trigger),
    }
    right_data = {
        "thumb_curl": float((r_skeletal or {}).get("thumb", previous_right["thumb_curl"])),
        "index_curl": float((r_skeletal or {}).get("index", previous_right["index_curl"])),
        "middle_curl": float((r_skeletal or {}).get("middle", previous_right["middle_curl"])),
        "ring_curl": float((r_skeletal or {}).get("ring", previous_right["ring_curl"])),
        "pinky_curl": float((r_skeletal or {}).get("pinky", previous_right["pinky_curl"])),
        TRIGGER_CHANNEL: float(right_trigger),
    }

    return {"left": left_data, "right": right_data}


def _get_poses(ovr_context: OVRContext) -> Generator[tuple[datetime.datetime, OVRTracker, Matrix], None, None]:
    system = openvr.VRSystem()
    poses, _ = openvr.VRCompositor().waitGetPoses([], None)
    time = datetime.datetime.now()

    for tracker in ovr_context.trackers:
        if not bool(system.isTrackedDeviceConnected(tracker.index)):
            continue

        absolute_pose = poses[tracker.index].mDeviceToAbsoluteTracking

        mat = Matrix([list(absolute_pose[0]), list(absolute_pose[1]), list(absolute_pose[2]), [0, 0, 0, 1]])
        mat_local = bpy_extras.io_utils.axis_conversion("Z", "Y", "Y", "Z").to_4x4()
        mat_local = mat_local @ mat

        yield time, tracker, mat_local


def _openvr_poll_thread_func(ovr_context: OVRContext):
    global pose_queue, stop_thread_flag, preview_buffer, record_buffer, input_record_buffer, buffer_lock, recording_active, latest_input_state

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

        input_state = _get_input(ovr_context)
        if input_state:
            with buffer_lock:
                latest_input_state = {
                    "left": input_state["left"].copy(),
                    "right": input_state["right"].copy(),
                }

        if recording_active:
            with buffer_lock:
                input_record_buffer.append((datetime.datetime.now(), {
                    "left": latest_input_state["left"].copy(),
                    "right": latest_input_state["right"].copy(),
                }))


def _clear_buffer():
    global record_buffer, input_record_buffer, buffer_lock
    with buffer_lock:
        record_buffer.clear()
        input_record_buffer.clear()


def _get_buffer() -> list[list[tuple[datetime.datetime, OVRTracker, Matrix]]]:
    global record_buffer, buffer_lock

    with buffer_lock:
        buffer_copy = record_buffer.copy()

    return buffer_copy



def _get_input_buffer() -> list[tuple[datetime.datetime, dict[str, dict[str, float]]]]:
    global input_record_buffer, buffer_lock

    with buffer_lock:
        buffer_copy = input_record_buffer.copy()

    return buffer_copy

def _get_latest_poses() -> list[tuple[datetime.datetime, OVRTracker, Matrix]] | None:
    global preview_buffer, buffer_lock
    with buffer_lock:
        if len(preview_buffer) == 0:
            return None

        return preview_buffer[-1]


def _apply_poses():
    # During undo/redo Blender may not provide a valid screen, skip preview safely.
    try:
        screen = bpy.context.screen
    except Exception:
        return

    # Don't preview when playing, since a previous recording may interfere
    if not screen or screen.is_animation_playing:
        return

    pose_data = _get_latest_poses()
    if not pose_data:
        return

    root_obj = bpy.data.objects.get("OVR Root")
    root_world = root_obj.matrix_world.copy() if root_obj else Matrix.Identity(4)

    for time, tracker, pose in pose_data:
        try:
            tracker_name = tracker.name
        except ReferenceError:
            continue

        tracker_obj = bpy.data.objects.get(tracker_name)
        if not tracker_obj:
            continue

        tracker_obj.matrix_world = root_world @ pose


def _pose_vis_timer():
    _apply_poses()
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return 1.0 / 60

    ovr_context = getattr(scene, "OVRContext", None)
    if ovr_context:
        with buffer_lock:
            input_state = {
                "left": latest_input_state["left"].copy(),
                "right": latest_input_state["right"].copy(),
            }
        _handle_input(ovr_context, input_state)

    return 1.0 / 60  # 60hz


def _insert_action(ovr_context: OVRContext):
    pose_data = _get_buffer()
    input_data = _get_input_buffer()

    num_pose_samples = len(pose_data)
    num_input_samples = len(input_data)
    print(f"OpenVR Processing {num_pose_samples} pose samples and {num_input_samples} finger samples")

    if num_pose_samples == 0 and num_input_samples == 0:
        print("OpenVR Found no samples to process")
        return

    if num_pose_samples > 0:
        take_start_time = pose_data[0][0][0]
    else:
        take_start_time = input_data[0][0]
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
        _set_action_slot_if_supported(tracker_obj, action)

    if num_input_samples > 0:
        root_obj = _get_or_create_root_object()

        if root_obj.animation_data is None:
            root_obj.animation_data_create()

        input_action = root_obj.animation_data.action
        if not input_action:
            input_action = bpy.data.actions.new(name="OVR_Root_Input_Action")
            root_obj.animation_data.action = input_action

        input_frames = []
        input_channels = {
            "left_thumb_curl": [],
            "left_index_curl": [],
            "left_middle_curl": [],
            "left_ring_curl": [],
            "left_pinky_curl": [],
            "right_thumb_curl": [],
            "right_index_curl": [],
            "right_middle_curl": [],
            "right_ring_curl": [],
            "right_pinky_curl": [],
        }

        for sample_time, sample_values in input_data:
            time_delta = sample_time - take_start_time
            frame = start_frame + time_delta.total_seconds() * framerate
            input_frames.append(frame)

            left_values = sample_values.get("left", {})
            right_values = sample_values.get("right", {})

            input_channels["left_thumb_curl"].append(left_values.get("thumb_curl", 0.0))
            input_channels["left_index_curl"].append(left_values.get("index_curl", 0.0))
            input_channels["left_middle_curl"].append(left_values.get("middle_curl", 0.0))
            input_channels["left_ring_curl"].append(left_values.get("ring_curl", 0.0))
            input_channels["left_pinky_curl"].append(left_values.get("pinky_curl", 0.0))
            input_channels["right_thumb_curl"].append(right_values.get("thumb_curl", 0.0))
            input_channels["right_index_curl"].append(right_values.get("index_curl", 0.0))
            input_channels["right_middle_curl"].append(right_values.get("middle_curl", 0.0))
            input_channels["right_ring_curl"].append(right_values.get("ring_curl", 0.0))
            input_channels["right_pinky_curl"].append(right_values.get("pinky_curl", 0.0))

        for channel, values in input_channels.items():
            if channel not in root_obj:
                root_obj[channel] = 0.0

            data_path = f'["{channel}"]'
            fcurve = input_action.fcurves.find(data_path, index=0)
            if fcurve:
                input_action.fcurves.remove(fcurve)
            fcurve = input_action.fcurves.new(data_path, index=0)
            fcurve.keyframe_points.add(len(input_frames))

            key_coords = [0.0] * (len(input_frames) * 2)
            key_coords[0::2] = input_frames
            key_coords[1::2] = values
            fcurve.keyframe_points.foreach_set("co", key_coords)
            fcurve.update()

            if values:
                root_obj[channel] = values[-1]

        _set_action_slot_if_supported(root_obj, input_action)

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
    if ovr_context:
        ovr_context.recordings_made = True
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
        if not bool(system.isTrackedDeviceConnected(i)):
            continue

        device_class = system.getTrackedDeviceClass(i)
        if device_class == openvr.TrackedDeviceClass_Invalid:
            continue

        tracker_serial = system.getStringTrackedDeviceProperty(i, openvr.Prop_SerialNumber_String)
        tracker = ovr_context.trackers.add()
        tracker.name = tracker_serial
        tracker.prev_name = tracker_serial
        tracker.serial = tracker_serial
        tracker.type = str(device_class)
        tracker.index = i
        tracker.connected = True
