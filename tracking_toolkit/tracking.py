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
input_source_handles = {"left": 0, "right": 0}
trigger_click_state = {"left": False, "right": False, "interact_ui": False, "event_left": False, "event_right": False}
trigger_debug_counter = 0

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

    def _unwrap_handle(value):
        if isinstance(value, tuple):
            if not value:
                return None
            for candidate in reversed(value):
                if isinstance(candidate, int):
                    return candidate
            return None
        return value

    def _get_action_set_handle(action_set_path: str):
        get_set_fn = getattr(vr_ipt, "getActionSetHandle", None) or getattr(vr_ipt, "GetActionSetHandle", None)
        if not get_set_fn:
            return None
        try:
            return _unwrap_handle(get_set_fn(action_set_path))
        except Exception:
            return None

    def _get_action_handle(action_path: str):
        get_action_fn = getattr(vr_ipt, "getActionHandle", None) or getattr(vr_ipt, "GetActionHandle", None)
        if not get_action_fn:
            return None
        variants = [action_path, action_path.lower()]
        for variant in variants:
            try:
                handle = _unwrap_handle(get_action_fn(variant))
            except Exception:
                continue
            if handle:
                return handle
        return None

    def _get_input_source_handle(source_path: str):
        get_source_fn = getattr(vr_ipt, "getInputSourceHandle", None) or getattr(vr_ipt, "GetInputSourceHandle", None)
        if not get_source_fn:
            return 0
        variants = [source_path, source_path.lower()]
        for variant in variants:
            try:
                handle = _unwrap_handle(get_source_fn(variant))
            except Exception:
                continue
            if handle:
                return int(handle)
        return 0

    global action_sets
    action_set_handle = _get_action_set_handle("/actions/default")
    action_sets = (openvr.VRActiveActionSet_t * 1)()
    action_sets[0].ulActionSet = int(action_set_handle or 0)

    global action_handles
    action_handles = {
        "l_skeleton": _get_action_handle("/actions/default/in/SkeletonLeftHand"),
        "r_skeleton": _get_action_handle("/actions/default/in/SkeletonRightHand"),
        "l_trigger_click": _get_action_handle("/actions/default/in/TriggerClickLeft"),
        "r_trigger_click": _get_action_handle("/actions/default/in/TriggerClickRight"),
        "interact_ui": _get_action_handle("/actions/default/in/InteractUI"),
    }

    global input_source_handles
    input_source_handles = {
        "left": _get_input_source_handle("/user/hand/left"),
        "right": _get_input_source_handle("/user/hand/right"),
    }

    print(f"[OpenVR] Action set '/actions/default' handle: {action_sets[0].ulActionSet}")
    print(f"[OpenVR] Trigger handles: left={action_handles.get('l_trigger_click')} right={action_handles.get('r_trigger_click')} interact_ui={action_handles.get('interact_ui')}")
    print(f"[OpenVR] Input source handles: left={input_source_handles.get('left')} right={input_source_handles.get('right')}")


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
    def _unwrap_openvr_result(result):
        if isinstance(result, tuple):
            for item in result:
                if hasattr(item, "bState") or hasattr(item, "bActive") or hasattr(item, "ulButtonPressed"):
                    return item
            if result:
                return result[-1]
        return result

    def _read_bool_attr(data, names: tuple[str, ...], default=False) -> bool:
        for name in names:
            try:
                if hasattr(data, name):
                    return bool(getattr(data, name))
            except Exception:
                continue
        return default

    def _digital_state(action_key: str, source_handle: int = 0) -> tuple[bool, bool, bool]:
        action = action_handles.get(action_key)
        if action is None or int(action) == 0:
            return False, False, False

        get_digital_fn = getattr(vr_ipt, "getDigitalActionData", None) or getattr(vr_ipt, "GetDigitalActionData", None)
        if not get_digital_fn:
            return False, False, False

        call_variants = [
            lambda: get_digital_fn(action),
            lambda: get_digital_fn(action, openvr.k_ulInvalidInputValueHandle),
        ]
        if source_handle:
            call_variants.append(lambda: get_digital_fn(action, int(source_handle)))

        for call in call_variants:
            try:
                data = _unwrap_openvr_result(call())
            except Exception:
                continue
            if not data:
                continue
            active = _read_bool_attr(data, ("bActive", "active"), default=True)
            state = _read_bool_attr(data, ("bState", "state"), default=False)
            changed = _read_bool_attr(data, ("bChanged", "changed"), default=False)
            return active, state, changed

        return False, False, False

    global trigger_debug_counter
    trigger_debug_counter += 1

    left_source = int(input_source_handles.get("left", 0) or 0)
    right_source = int(input_source_handles.get("right", 0) or 0)

    l_active, l_state, l_changed = _digital_state("l_trigger_click", left_source)
    r_active, r_state, r_changed = _digital_state("r_trigger_click", right_source)

    # InteractUI from either hand (often mapped only to one side in manifests)
    i_l_active, i_l_state, i_l_changed = _digital_state("interact_ui", left_source)
    i_r_active, i_r_state, i_r_changed = _digital_state("interact_ui", right_source)

    left_pressed = bool((l_active and l_state) or (i_l_active and i_l_state))
    right_pressed = bool((r_active and r_state) or (i_r_active and i_r_state))

    if l_changed and l_state:
        print("[OpenVR] Trigger click fired (digital l_trigger_click)")
    if r_changed and r_state:
        print("[OpenVR] Trigger click fired (digital r_trigger_click)")
    if i_l_changed and i_l_state:
        print("[OpenVR] Trigger click fired (digital interact_ui left)")
    if i_r_changed and i_r_state:
        print("[OpenVR] Trigger click fired (digital interact_ui right)")

    if left_pressed or right_pressed or (trigger_debug_counter % 120 == 0):
        print(
            "[OpenVR][TriggerDebug][Digital] "
            f"l={{a:{l_active},s:{l_state},c:{l_changed}}} "
            f"r={{a:{r_active},s:{r_state},c:{r_changed}}} "
            f"il={{a:{i_l_active},s:{i_l_state},c:{i_l_changed}}} "
            f"ir={{a:{i_r_active},s:{i_r_state},c:{i_r_changed}}}"
        )

    # Legacy path analogous to finger fallback sampling: poll controller state directly each frame.
    try:
        system = openvr.VRSystem()
        get_idx_fn = getattr(system, "getTrackedDeviceIndexForControllerRole", None) or getattr(system, "GetTrackedDeviceIndexForControllerRole", None)
        get_state_fn = getattr(system, "getControllerState", None) or getattr(system, "GetControllerState", None)

        left_role = getattr(openvr, "TrackedControllerRole_LeftHand", 1)
        right_role = getattr(openvr, "TrackedControllerRole_RightHand", 2)
        trigger_button = getattr(openvr, "k_EButton_SteamVR_Trigger", 33)
        trigger_mask = 1 << trigger_button

        def _read_role_pressed(role_const: int):
            if not get_idx_fn or not get_state_fn:
                return False, 0.0, -1
            try:
                idx = int(get_idx_fn(role_const))
            except Exception:
                return False, 0.0, -1
            if idx < 0 or idx == getattr(openvr, "k_unTrackedDeviceIndexInvalid", 0xFFFFFFFF):
                return False, 0.0, idx

            state_obj = None
            calls = [
                lambda: get_state_fn(idx),
                lambda: get_state_fn(idx, openvr.VRControllerState_t()),
            ]
            for c in calls:
                try:
                    state_obj = _unwrap_openvr_result(c())
                except Exception:
                    continue
                if state_obj:
                    break
            if not state_obj:
                return False, 0.0, idx

            mask_pressed = bool(getattr(state_obj, "ulButtonPressed", 0) & trigger_mask)
            axis_val = 0.0
            axes = getattr(state_obj, "rAxis", None)
            if axes:
                for axis_idx in (1, 2, 0):
                    try:
                        axis = axes[axis_idx]
                        axis_val = max(axis_val, float(getattr(axis, "x", 0.0)), float(getattr(axis, "y", 0.0)))
                    except Exception:
                        continue
            axis_pressed = axis_val >= 0.75
            return bool(mask_pressed or axis_pressed), axis_val, idx

        legacy_left, legacy_left_axis, left_idx = _read_role_pressed(left_role)
        legacy_right, legacy_right_axis, right_idx = _read_role_pressed(right_role)

        if legacy_left or legacy_right or (trigger_debug_counter % 120 == 0):
            print(
                "[OpenVR][TriggerDebug][ControllerState] "
                f"left(idx={left_idx},pressed={legacy_left},axis={legacy_left_axis:.3f}) "
                f"right(idx={right_idx},pressed={legacy_right},axis={legacy_right_axis:.3f})"
            )

        left_pressed = bool(left_pressed or legacy_left)
        right_pressed = bool(right_pressed or legacy_right)
    except Exception:
        pass

    # Event path (optional third source)
    try:
        system = openvr.VRSystem()
        poll_event_fn = getattr(system, "pollNextEvent", None) or getattr(system, "PollNextEvent", None)
        if poll_event_fn:
            ev_press = getattr(openvr, "VREvent_ButtonPress", 200)
            ev_unpress = getattr(openvr, "VREvent_ButtonUnpress", 201)
            trig_btn = getattr(openvr, "k_EButton_SteamVR_Trigger", 33)
            get_role_fn = getattr(system, "getControllerRoleForTrackedDeviceIndex", None) or getattr(system, "GetControllerRoleForTrackedDeviceIndex", None)
            for _ in range(32):
                try:
                    raw = poll_event_fn(openvr.VREvent_t())
                except Exception:
                    try:
                        raw = poll_event_fn()
                    except Exception:
                        break
                if isinstance(raw, tuple):
                    has_event = bool(raw[0]) if raw else False
                    ev = raw[1] if len(raw) >= 2 else None
                else:
                    has_event = bool(raw)
                    ev = raw
                if not has_event:
                    break
                if not ev:
                    continue
                et = int(getattr(ev, "eventType", -1))
                if et not in (ev_press, ev_unpress):
                    continue
                btn = int(getattr(getattr(getattr(ev, "data", None), "controller", None), "button", -1))
                if btn != trig_btn:
                    continue
                idx = int(getattr(ev, "trackedDeviceIndex", -1))
                role = None
                if get_role_fn and idx >= 0:
                    try:
                        role = get_role_fn(idx)
                    except Exception:
                        role = None
                is_press = et == ev_press
                if role == getattr(openvr, "TrackedControllerRole_LeftHand", 1):
                    trigger_click_state["event_left"] = is_press
                    if is_press:
                        print(f"[OpenVR] Trigger click fired (event left idx={idx})")
                elif role == getattr(openvr, "TrackedControllerRole_RightHand", 2):
                    trigger_click_state["event_right"] = is_press
                    if is_press:
                        print(f"[OpenVR] Trigger click fired (event right idx={idx})")
    except Exception:
        pass

    left_pressed = bool(left_pressed or trigger_click_state.get("event_left", False))
    right_pressed = bool(right_pressed or trigger_click_state.get("event_right", False))

    current_states = {
        "left": left_pressed,
        "right": right_pressed,
        "interact_ui": bool((i_l_active and i_l_state) or (i_r_active and i_r_state)),
    }
    for key, is_pressed in current_states.items():
        was_pressed = trigger_click_state.get(key, False)
        if is_pressed and not was_pressed:
            print(f"[OpenVR] Trigger click fired (edge): {key}")
        trigger_click_state[key] = is_pressed

    if left_pressed or right_pressed or (trigger_debug_counter % 120 == 0):
        print(
            "[OpenVR][TriggerDebug][Final] "
            f"left={left_pressed} right={right_pressed} "
            f"event_left={trigger_click_state.get('event_left', False)} event_right={trigger_click_state.get('event_right', False)}"
        )

    return float(left_pressed), float(right_pressed)


def _get_input(ovr_context: OVRContext) -> dict[str, dict[str, float]] | None:
    vr_ipt = openvr.VRInput()

    updated = False
    if action_handles and action_sets:
        update_fn = getattr(vr_ipt, "updateActionState", None) or getattr(vr_ipt, "UpdateActionState", None)
        update_variants = []
        if update_fn:
            update_variants = [
                lambda: update_fn(action_sets, ctypes.sizeof(openvr.VRActiveActionSet_t), len(action_sets)),
                lambda: update_fn(action_sets),
                lambda: update_fn(action_sets[0], ctypes.sizeof(openvr.VRActiveActionSet_t), 1),
            ]

        last_update_error = None
        for update_call in update_variants:
            try:
                update_call()
                updated = True
                break
            except Exception as e:
                last_update_error = e
                continue
        if not updated:
            print(f"[OpenVR] updateActionState failed for all variants: {last_update_error}")

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
