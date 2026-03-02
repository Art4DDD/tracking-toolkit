import datetime
import re
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


def init_handles():
    vr_ipt = openvr.VRInput()

    global action_sets
    action_sets = (openvr.VRActiveActionSet_t * 1)()
    action_set = action_sets[0]
    action_set.ulActionSet = vr_ipt.getActionSetHandle("/actions/legacy")

    def _get_action_handle(*paths: str):
        for path in paths:
            try:
                return vr_ipt.getActionHandle(path)
            except Exception:
                continue

        return None

    global action_handles
    action_handles = {
        "l_joystick": _get_action_handle("/actions/legacy/in/Left_Axis0_Value"),
        "r_joystick": _get_action_handle("/actions/legacy/in/Right_Axis1_Value"),

        "l_trigger": _get_action_handle("/actions/legacy/in/Left_Axis1_Value"),
        "r_trigger": _get_action_handle("/actions/legacy/in/Right_Axis1_Value"),

        "l_grip": _get_action_handle("/actions/legacy/in/Left_Axis2_Value"),
        "r_grip": _get_action_handle("/actions/legacy/in/Right_Axis2_Value"),

        # Legacy finger actions can differ by driver/runtime naming, so keep fallbacks.
        "l_thumb": _get_action_handle("/actions/legacy/in/Left_Thumb_Value"),
        "r_thumb": _get_action_handle("/actions/legacy/in/Right_Thumb_Value"),
        "l_index": _get_action_handle("/actions/legacy/in/Left_Index_Value"),
        "r_index": _get_action_handle("/actions/legacy/in/Right_Index_Value"),
        "l_middle": _get_action_handle("/actions/legacy/in/Left_Middle_Value"),
        "r_middle": _get_action_handle("/actions/legacy/in/Right_Middle_Value"),
        "l_ring": _get_action_handle("/actions/legacy/in/Left_Ring_Value"),
        "r_ring": _get_action_handle("/actions/legacy/in/Right_Ring_Value"),
        "l_pinky": _get_action_handle("/actions/legacy/in/Left_Pinky_Value"),
        "r_pinky": _get_action_handle("/actions/legacy/in/Right_Pinky_Value"),

        "r_a": _get_action_handle("/actions/legacy/in/Right_A_Press"),
        "l_a": _get_action_handle("/actions/legacy/in/Left_A_Press"),

        "l_b": _get_action_handle("/actions/legacy/in/Left_ApplicationMenu_Press"),
        "r_b": _get_action_handle("/actions/legacy/in/Right_ApplicationMenu_Press")
    }

    print("Initialized OpenVR action handles")


def _handle_input(ovr_context: OVRContext):
    l_ipt = ovr_context.l_input
    r_ipt = ovr_context.r_input


def _get_input(ovr_context: OVRContext):
    if not (action_handles and action_sets):
        return

    vr_ipt = openvr.VRInput()
    l_ipt: OVRInput = ovr_context.l_input
    r_ipt: OVRInput = ovr_context.r_input

    vr_ipt.updateActionState(action_sets)

    def _analog_data(handle_key: str):
        handle = action_handles.get(handle_key)
        if handle is None:
            return None

        try:
            return vr_ipt.getAnalogActionData(handle, 0)
        except Exception:
            return None

    def _analog(handle_key: str, default: float = 0.0) -> float:
        data = _analog_data(handle_key)
        if data is None:
            return default

        return data.x

    def _digital(handle_key: str, default: bool = False) -> bool:
        handle = action_handles.get(handle_key)
        if handle is None:
            return default

        try:
            return vr_ipt.getDigitalActionData(handle, 0).bState
        except Exception:
            return default

    # Axis values
    l_joy = _analog_data("l_joystick")
    r_joy = _analog_data("r_joystick")
    l_ipt.joystick_position = ((l_joy.x if l_joy else 0.0), (l_joy.y if l_joy else 0.0))
    r_ipt.joystick_position = ((r_joy.x if r_joy else 0.0), (r_joy.y if r_joy else 0.0))

    l_ipt.trigger_strength = _analog("l_trigger")
    r_ipt.trigger_strength = _analog("r_trigger")

    l_ipt.grip_strength = _analog("l_grip")
    r_ipt.grip_strength = _analog("r_grip")

    l_ipt.thumb_strength = _analog("l_thumb")
    r_ipt.thumb_strength = _analog("r_thumb")

    l_ipt.index_strength = _analog("l_index", l_ipt.trigger_strength)
    r_ipt.index_strength = _analog("r_index", r_ipt.trigger_strength)

    l_ipt.middle_strength = _analog("l_middle", l_ipt.grip_strength)
    r_ipt.middle_strength = _analog("r_middle", r_ipt.grip_strength)

    l_ipt.ring_strength = _analog("l_ring", l_ipt.grip_strength)
    r_ipt.ring_strength = _analog("r_ring", r_ipt.grip_strength)

    l_ipt.pinky_strength = _analog("l_pinky", l_ipt.grip_strength)
    r_ipt.pinky_strength = _analog("r_pinky", r_ipt.grip_strength)

    l_ipt.a_button = _digital("l_a")
    r_ipt.a_button = _digital("r_a")

    l_ipt.b_button = _digital("l_b")
    r_ipt.b_button = _digital("r_b")


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
        #_handle_input(ovr_context)


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

    synthetic_serial_pattern = re.compile(r"^LHR-FFFFFFF[0-9A-F]+$", re.IGNORECASE)

    def _get_prop(index: int, prop: int) -> str:
        try:
            value = system.getStringTrackedDeviceProperty(index, prop)
            return value.strip() if value else ""
        except Exception:
            return ""

    for i in range(openvr.k_unMaxTrackedDeviceCount):
        device_class = system.getTrackedDeviceClass(i)
        if device_class == openvr.TrackedDeviceClass_Invalid:
            continue

        tracker_serial = _get_prop(i, openvr.Prop_SerialNumber_String)

        # Keep the headset, but normalize synthetic serial-like names to a readable HMD model name.
        if device_class == openvr.TrackedDeviceClass_HMD and synthetic_serial_pattern.match(tracker_serial):
            model_number = _get_prop(i, openvr.Prop_ModelNumber_String)
            render_model = _get_prop(i, openvr.Prop_RenderModelName_String)
            tracking_system = _get_prop(i, openvr.Prop_TrackingSystemName_String)
            tracker_name = model_number or render_model or tracking_system or "HMD"
        else:
            tracker_name = tracker_serial

        if not tracker_name:
            tracker_name = f"TrackedDevice_{i}"

        tracker = ovr_context.trackers.add()
        tracker.name = tracker_name
        tracker.prev_name = tracker_name
        tracker.serial = tracker_serial
        tracker.type = str(device_class)
        tracker.index = i
        tracker.connected = bool(system.isTrackedDeviceConnected(i))  # Just in case, do it for both
