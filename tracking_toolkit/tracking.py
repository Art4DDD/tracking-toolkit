import datetime
import ctypes
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
input_record_buffer = []
buffer_lock = threading.Lock()
recording_active = False

action_sets = []
action_handles = {}
active_ovr_context = None
last_tracker_append_check = datetime.datetime.min

FINGER_CHANNELS = ("thumb_curl", "index_curl", "middle_curl", "ring_curl", "pinky_curl")

FINGER_BONE_CHAINS = {
    "thumb": (2, 3, 4, 5),
    "index": (6, 7, 8, 9, 10),
    "middle": (11, 12, 13, 14, 15),
    "ring": (16, 17, 18, 19, 20),
    "pinky": (21, 22, 23, 24, 25),
}


DEBUG_STRING_PROP_SUFFIX = "_String"
DEBUG_INT_PROP_SUFFIX = "_Int32"
DEBUG_UINT64_PROP_SUFFIX = "_Uint64"
DEBUG_BOOL_PROP_SUFFIX = "_Bool"
DEBUG_FLOAT_PROP_SUFFIX = "_Float"


def _iter_openvr_property_ids_by_suffix(suffix: str) -> list[tuple[str, int]]:
    props: list[tuple[str, int]] = []
    for attr in dir(openvr):
        if not attr.startswith("Prop_") or not attr.endswith(suffix):
            continue
        prop_id = getattr(openvr, attr, None)
        if isinstance(prop_id, int):
            props.append((attr, prop_id))
    props.sort(key=lambda x: x[0])
    return props


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

def _get_tracker_live_object(tracker: OVRTracker) -> bpy.types.Object | None:
    if tracker.target.object:
        return tracker.target.object

    existing_obj = bpy.data.objects.get(tracker.name)
    if existing_obj:
        tracker.target.object = existing_obj
        return existing_obj

    return None


def _find_existing_controller_object(hand: str) -> bpy.types.Object | None:
    hand = hand.lower()
    candidates = {
        "left": ["left controller", "controller left", "controller_l", "controller.l", "left_hand", "l_hand", "index left", "knuckles left"],
        "right": ["right controller", "controller right", "controller_r", "controller.r", "right_hand", "r_hand", "index right", "knuckles right"],
    }

    names = candidates.get(hand, [])
    for obj in bpy.data.objects:
        name = obj.name.lower()
        if any(token in name for token in names):
            return obj

    return None




def _is_virtual_lhr_controller(tracker: OVRTracker) -> bool:
    name = (tracker.name or "").upper()
    return name.startswith("LHR-FFFFFF")


def _safe_device_property(system, device_index: int, property_id: int) -> str:
    try:
        return system.getStringTrackedDeviceProperty(device_index, property_id)
    except Exception:
        return ""


def _normalize_tracker_name(raw_name: str) -> str:
    normalized = " ".join((raw_name or "").replace("\n", " ").replace("\r", " ").split())
    return normalized.strip()


def _resolve_tracker_name(system, device_index: int, serial: str) -> str:
    serial_clean = " ".join((serial or "").replace("\n", " ").replace("\r", " ").split())
    if serial_clean:
        return serial_clean

    return f"Device {device_index}"




def _extract_input_profile_info(raw_input_profile_path: str) -> dict[str, str]:
    raw = (raw_input_profile_path or "").strip()
    if not raw:
        return {}

    info: dict[str, str] = {
        "input_profile_path_raw": raw,
    }

    normalized = raw.replace("\\", "/")
    if normalized.startswith("{"):
        end_idx = normalized.find("}")
        if end_idx > 1:
            info["input_profile_driver"] = normalized[1:end_idx]
            info["input_profile_driver_relative_path"] = normalized[end_idx + 1:].lstrip("/")

    return info




def _extract_driver_metadata(fields: dict[str, str]) -> dict[str, str]:
    info: dict[str, str] = {}

    registered = (fields.get("registereddevicetype") or "").strip()
    if registered:
        info["registered_device_type_raw"] = registered
        # Usually looks like: "<driver>/<device>".
        if "/" in registered:
            driver, _, device = registered.partition("/")
            if driver:
                info["registered_device_driver"] = driver
            if device:
                info["registered_device_name"] = device

    resource_root = (fields.get("resourceroot") or "").strip()
    if resource_root:
        info["resource_root_raw"] = resource_root
        normalized = resource_root.replace("\\", "/")
        if normalized.startswith("{") and "}" in normalized:
            end_idx = normalized.find("}")
            if end_idx > 1:
                info["resource_root_driver"] = normalized[1:end_idx]
                info["resource_root_relative_path"] = normalized[end_idx + 1:].lstrip("/")
        else:
            # Some drivers expose plain id here, e.g. "lighthouse", "pico".
            info["resource_root_driver"] = normalized.split("/")[0]

    tracking_system = (fields.get("trackingsystemname") or "").strip()
    if tracking_system:
        info["tracking_system_name"] = tracking_system

    driver_version = (fields.get("driverversion") or "").strip()
    if driver_version:
        info["driver_version"] = driver_version

    return info





def _infer_device_driver_id(fields: dict[str, str]) -> str:
    input_profile_driver = (fields.get("input_profile_driver") or "").strip()
    if input_profile_driver:
        return input_profile_driver

    registered = (fields.get("registereddevicetype") or "").strip()
    if "/" in registered:
        driver, _, _ = registered.partition("/")
        if driver:
            return driver

    resource_root = (fields.get("resourceroot") or "").strip()
    if resource_root:
        normalized = resource_root.replace("\\", "/")
        if normalized.startswith("{") and "}" in normalized:
            end_idx = normalized.find("}")
            if end_idx > 1:
                return normalized[1:end_idx]
        return normalized.split("/")[0]

    return ""

def _collect_driver_registry_info() -> dict[str, str]:
    info: dict[str, str] = {}

    driver_manager_factory = getattr(openvr, "VRDriverManager", None)
    if not driver_manager_factory:
        return info

    try:
        driver_manager = driver_manager_factory()
    except Exception:
        return info

    get_count = (
        getattr(driver_manager, "getDriverCount", None)
        or getattr(driver_manager, "GetDriverCount", None)
    )
    get_name = (
        getattr(driver_manager, "getDriverName", None)
        or getattr(driver_manager, "GetDriverName", None)
    )
    if not get_count or not get_name:
        return info

    try:
        count = int(get_count())
    except Exception:
        return info

    names: list[str] = []
    for idx in range(max(count, 0)):
        try:
            value = get_name(idx)
        except Exception:
            continue

        if isinstance(value, tuple):
            name = next((item for item in value if isinstance(item, str)), "")
        else:
            name = value if isinstance(value, str) else ""

        name = (name or "").strip()
        if name:
            names.append(name)

    if names:
        uniq = sorted(set(names))
        info["driver_registry_count"] = str(len(uniq))
        info["driver_registry_names"] = ",".join(uniq)

    return info


def _collect_render_model_registry_info() -> dict[str, str]:
    info: dict[str, str] = {}

    render_models_factory = getattr(openvr, "VRRenderModels", None)
    if not render_models_factory:
        return info

    try:
        render_models = render_models_factory()
    except Exception:
        return info

    get_count = (
        getattr(render_models, "getRenderModelCount", None)
        or getattr(render_models, "GetRenderModelCount", None)
    )
    get_name = (
        getattr(render_models, "getRenderModelName", None)
        or getattr(render_models, "GetRenderModelName", None)
    )

    if not get_count or not get_name:
        return info

    try:
        count = int(get_count())
    except Exception:
        return info

    info["render_model_registry_count"] = str(max(count, 0))

    sample: list[str] = []
    for idx in range(min(max(count, 0), 40)):
        try:
            value = get_name(idx)
        except Exception:
            continue

        if isinstance(value, tuple):
            name = next((item for item in value if isinstance(item, str)), "")
        else:
            name = value if isinstance(value, str) else ""
        name = (name or "").strip()
        if name:
            sample.append(name)

    if sample:
        info["render_model_registry_sample"] = ",".join(sample)

    return info

def _collect_tracker_debug_info(system, device_index: int, device_class: int) -> dict[str, str]:
    fields = {
        "index": str(device_index),
        "class": str(device_class),
        "connected": str(bool(system.isTrackedDeviceConnected(device_index))),
        "serial_raw": _safe_device_property(system, device_index, openvr.Prop_SerialNumber_String),
    }

    for prop_name, prop_id in _iter_openvr_property_ids_by_suffix(DEBUG_STRING_PROP_SUFFIX):
        prop_value = _normalize_tracker_name(_safe_device_property(system, device_index, prop_id))
        if prop_value:
            fields[prop_name.replace("Prop_", "").replace(DEBUG_STRING_PROP_SUFFIX, "").lower()] = prop_value

    for prop_name, prop_id in _iter_openvr_property_ids_by_suffix(DEBUG_INT_PROP_SUFFIX):
        try:
            prop_value = system.getInt32TrackedDeviceProperty(device_index, prop_id)
            fields[prop_name.replace("Prop_", "").replace(DEBUG_INT_PROP_SUFFIX, "").lower()] = str(int(prop_value))
        except Exception:
            continue

    for prop_name, prop_id in _iter_openvr_property_ids_by_suffix(DEBUG_UINT64_PROP_SUFFIX):
        try:
            prop_value = system.getUint64TrackedDeviceProperty(device_index, prop_id)
            fields[prop_name.replace("Prop_", "").replace(DEBUG_UINT64_PROP_SUFFIX, "").lower()] = str(int(prop_value))
        except Exception:
            continue

    for prop_name, prop_id in _iter_openvr_property_ids_by_suffix(DEBUG_BOOL_PROP_SUFFIX):
        try:
            prop_value = system.getBoolTrackedDeviceProperty(device_index, prop_id)
            fields[prop_name.replace("Prop_", "").replace(DEBUG_BOOL_PROP_SUFFIX, "").lower()] = str(bool(prop_value))
        except Exception:
            continue

    for prop_name, prop_id in _iter_openvr_property_ids_by_suffix(DEBUG_FLOAT_PROP_SUFFIX):
        try:
            prop_value = system.getFloatTrackedDeviceProperty(device_index, prop_id)
            fields[prop_name.replace("Prop_", "").replace(DEBUG_FLOAT_PROP_SUFFIX, "").lower()] = f"{float(prop_value):.6f}"
        except Exception:
            continue

    input_profile_path = fields.get("inputprofilepath") or fields.get("inputprofilepath_string")
    if input_profile_path:
        fields.update(_extract_input_profile_info(input_profile_path))

    fields.update(_extract_driver_metadata(fields))

    inferred_driver_id = _infer_device_driver_id(fields)
    if inferred_driver_id:
        fields["device_driver_id"] = inferred_driver_id

    driver_registry = _collect_driver_registry_info()
    fields.update(driver_registry)
    if inferred_driver_id and driver_registry.get("driver_registry_names"):
        known = {name.strip() for name in driver_registry["driver_registry_names"].split(",") if name.strip()}
        fields["device_driver_registered"] = str(inferred_driver_id in known)

    fields.update(_collect_render_model_registry_info())

    return fields


def _format_tracker_debug_info(debug_info: dict[str, str]) -> str:
    return " | ".join(f"{key}={value}" for key, value in debug_info.items())


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
    controller_targets = _resolve_controller_targets(ovr_context)

    left_obj = controller_targets.get("left")
    if left_obj:
        _ensure_finger_properties(left_obj)
        left_obj["thumb_curl"] = ovr_context.l_input.thumb_curl
        left_obj["index_curl"] = ovr_context.l_input.index_curl
        left_obj["middle_curl"] = ovr_context.l_input.middle_curl
        left_obj["ring_curl"] = ovr_context.l_input.ring_curl
        left_obj["pinky_curl"] = ovr_context.l_input.pinky_curl

    right_obj = controller_targets.get("right")
    if right_obj:
        _ensure_finger_properties(right_obj)
        right_obj["thumb_curl"] = ovr_context.r_input.thumb_curl
        right_obj["index_curl"] = ovr_context.r_input.index_curl
        right_obj["middle_curl"] = ovr_context.r_input.middle_curl
        right_obj["ring_curl"] = ovr_context.r_input.ring_curl
        right_obj["pinky_curl"] = ovr_context.r_input.pinky_curl


def _get_input(ovr_context: OVRContext):
    if not (action_handles and action_sets):
        return

    vr_ipt = openvr.VRInput()
    l_ipt: OVRInput = ovr_context.l_input
    r_ipt: OVRInput = ovr_context.r_input

    updated = False
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

    if not updated:
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

        get_action_data_fn = getattr(vr_ipt, "getSkeletalActionData", None) or getattr(vr_ipt, "GetSkeletalActionData", None)
        if not get_action_data_fn:
            return None

        try:
            action_data = get_action_data_fn(action)
        except Exception:
            return None

        if not getattr(action_data, "bActive", False):
            return None

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
                return {
                    "thumb": float(curls[0]),
                    "index": float(curls[1]),
                    "middle": float(curls[2]),
                    "ring": float(curls[3]),
                    "pinky": float(curls[4]),
                }

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




def _snapshot_input_state(ovr_context: OVRContext):
    l_ipt: OVRInput = ovr_context.l_input
    r_ipt: OVRInput = ovr_context.r_input

    return datetime.datetime.now(), {
        "left": {
            "thumb_curl": l_ipt.thumb_curl,
            "index_curl": l_ipt.index_curl,
            "middle_curl": l_ipt.middle_curl,
            "ring_curl": l_ipt.ring_curl,
            "pinky_curl": l_ipt.pinky_curl,
        },
        "right": {
            "thumb_curl": r_ipt.thumb_curl,
            "index_curl": r_ipt.index_curl,
            "middle_curl": r_ipt.middle_curl,
            "ring_curl": r_ipt.ring_curl,
            "pinky_curl": r_ipt.pinky_curl,
        },
    }


def _resolve_controller_targets(ovr_context: OVRContext) -> dict[str, bpy.types.Object]:
    system = openvr.VRSystem()

    role_prop = openvr.Prop_ControllerRoleHint_Int32
    left_role = getattr(openvr, "TrackedControllerRole_LeftHand", 1)
    right_role = getattr(openvr, "TrackedControllerRole_RightHand", 2)

    # Prefer already existing scene controller objects (not autogenerated LHR-* refs)
    targets = {
        "left": _find_existing_controller_object("left"),
        "right": _find_existing_controller_object("right"),
    }
    targets = {k: v for k, v in targets.items() if v is not None}

    unresolved = []
    for tracker in ovr_context.trackers:
        if tracker.type != str(openvr.TrackedDeviceClass_Controller):
            continue
        target_obj = _get_tracker_live_object(tracker)
        if not target_obj:
            continue

        try:
            role = system.getInt32TrackedDeviceProperty(tracker.index, role_prop)
        except Exception:
            role = None

        if role == left_role and "left" not in targets:
            targets["left"] = target_obj
        elif role == right_role and "right" not in targets:
            targets["right"] = target_obj
        else:
            unresolved.append((tracker, target_obj))

    if "left" not in targets or "right" not in targets:
        for tracker, target_obj in unresolved:
            tracker_name = tracker.name.lower()
            if "left" not in targets and ("left" in tracker_name or "_l" in tracker_name or tracker_name.endswith(".l")):
                targets["left"] = target_obj
            elif "right" not in targets and ("right" in tracker_name or "_r" in tracker_name or tracker_name.endswith(".r")):
                targets["right"] = target_obj

    return targets

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
    global pose_queue, stop_thread_flag, preview_buffer, record_buffer, input_record_buffer, buffer_lock, recording_active

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

        if recording_active:
            input_sample = _snapshot_input_state(ovr_context)
            with buffer_lock:
                input_record_buffer.append(input_sample)


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
    global active_ovr_context, last_tracker_append_check

    _apply_poses()

    # Auto-append newly connected devices while VR is running (SteamVR-like behavior).
    if active_ovr_context and getattr(active_ovr_context, "enabled", False):
        now = datetime.datetime.now(datetime.timezone.utc)
        if (now - last_tracker_append_check).total_seconds() >= 2.0:
            append_trackers(active_ovr_context)
            last_tracker_append_check = now

    return 1.0 / 60  # 60hz


def _insert_action(ovr_context: OVRContext):
    pose_data = _get_buffer()
    input_data = _get_input_buffer()

    num_pose_samples = len(pose_data)
    num_input_samples = len(input_data)
    print(f"OpenVR Processing {num_pose_samples} pose samples and {num_input_samples} finger samples")

    if num_pose_samples == 0 and num_input_samples == 0:
        print(f"OpenVR Found no samples to process")
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
        controller_targets = _resolve_controller_targets(ovr_context)
        input_frames = []
        hand_channel_values = {
            "left": {channel: [] for channel in FINGER_CHANNELS},
            "right": {channel: [] for channel in FINGER_CHANNELS},
        }

        for sample_time, sample_values in input_data:
            time_delta = sample_time - take_start_time
            frame = start_frame + time_delta.total_seconds() * framerate
            input_frames.append(frame)

            for hand in ("left", "right"):
                hand_values = sample_values.get(hand, {})
                for channel in FINGER_CHANNELS:
                    hand_channel_values[hand][channel].append(hand_values.get(channel, 0.0))

        for hand, target_obj in controller_targets.items():
            if target_obj.animation_data is None:
                target_obj.animation_data_create()

            action = target_obj.animation_data.action
            if not action:
                action = bpy.data.actions.new(name=f"{target_obj.name}_Input_Action")
                target_obj.animation_data.action = action

            _ensure_finger_properties(target_obj)
            for channel in FINGER_CHANNELS:

                data_path = f'["{channel}"]'

                fcurve = action.fcurves.find(data_path, index=0)
                if fcurve:
                    action.fcurves.remove(fcurve)
                fcurve = action.fcurves.new(data_path, index=0)
                fcurve.keyframe_points.add(len(input_frames))

                values = hand_channel_values[hand][channel]

                key_coords = [0.0] * (len(input_frames) * 2)
                key_coords[0::2] = input_frames
                key_coords[1::2] = values
                fcurve.keyframe_points.foreach_set("co", key_coords)
                fcurve.update()

                if values:
                    target_obj[channel] = values[-1]

            _set_action_slot_if_supported(target_obj, action)

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
    global polling_thread, stop_thread_flag, active_ovr_context, last_tracker_append_check

    if polling_thread and polling_thread.is_alive():
        stop_thread_flag.set()
        polling_thread.join()

    stop_thread_flag.clear()
    active_ovr_context = ovr_context
    last_tracker_append_check = datetime.datetime.min
    polling_thread = threading.Thread(target=lambda: _openvr_poll_thread_func(ovr_context))
    polling_thread.daemon = True  # Quit with Blender
    polling_thread.start()

    if not bpy.app.timers.is_registered(_pose_vis_timer):
        bpy.app.timers.register(_pose_vis_timer)

    print("OpenVR Preview Started")


def stop_preview():
    global polling_thread, stop_thread_flag, preview_buffer, active_ovr_context

    if bpy.app.timers.is_registered(_pose_vis_timer):
        bpy.app.timers.unregister(_pose_vis_timer)

    if polling_thread and polling_thread.is_alive():
        stop_thread_flag.set()
        polling_thread.join()

    with buffer_lock:
        preview_buffer.clear()

    polling_thread = None
    active_ovr_context = None
    stop_thread_flag.clear()

    print("OpenVR Preview Stopped")



def load_trackers(ovr_context: OVRContext):
    print("OpenVR Loading Trackers")
    system = openvr.VRSystem()

    ovr_context.trackers.clear()

    for i in range(openvr.k_unMaxTrackedDeviceCount):
        if system.getTrackedDeviceClass(i) == openvr.TrackedDeviceClass_Invalid:
            continue

        device_class = system.getTrackedDeviceClass(i)
        tracker_serial = _safe_device_property(system, i, openvr.Prop_SerialNumber_String)
        tracker_name = _resolve_tracker_name(system, i, tracker_serial)

        existing_names = {t.name for t in ovr_context.trackers}
        unique_name = tracker_name
        suffix = 2
        while unique_name in existing_names:
            unique_name = f"{tracker_name} {suffix}"
            suffix += 1

        tracker = ovr_context.trackers.add()
        tracker.name = unique_name
        tracker.prev_name = unique_name
        tracker.serial = tracker_serial
        tracker.type = str(device_class)
        tracker.index = i
        tracker.connected = bool(system.isTrackedDeviceConnected(i))  # Just in case, do it for both

        debug_info = _collect_tracker_debug_info(system, i, device_class)
        if debug_info:
            print(f"[OpenVR Device] {_format_tracker_debug_info(debug_info)}")


def append_trackers(ovr_context: OVRContext):
    """Append new devices and refresh connection state without clearing existing list."""
    print("OpenVR Appending Trackers")
    system = openvr.VRSystem()

    trackers_by_index = {tracker.index: tracker for tracker in ovr_context.trackers}

    for i in range(openvr.k_unMaxTrackedDeviceCount):
        device_class = system.getTrackedDeviceClass(i)
        if device_class == openvr.TrackedDeviceClass_Invalid:
            continue

        tracker_serial = _safe_device_property(system, i, openvr.Prop_SerialNumber_String)
        connected = bool(system.isTrackedDeviceConnected(i))

        existing = trackers_by_index.get(i)
        if existing:
            existing.connected = connected
            if tracker_serial:
                existing.serial = tracker_serial
            continue

        tracker_name = _resolve_tracker_name(system, i, tracker_serial)

        existing_names = {t.name for t in ovr_context.trackers}
        unique_name = tracker_name
        suffix = 2
        while unique_name in existing_names:
            unique_name = f"{tracker_name} {suffix}"
            suffix += 1

        tracker = ovr_context.trackers.add()
        tracker.name = unique_name
        tracker.prev_name = unique_name
        tracker.serial = tracker_serial
        tracker.type = str(device_class)
        tracker.index = i
        tracker.connected = connected

        debug_info = _collect_tracker_debug_info(system, i, device_class)
        if debug_info:
            print(f"[OpenVR Device] {_format_tracker_debug_info(debug_info)}")
