import bpy
import openvr
from pathlib import Path

from .properties import Preferences, OVRContext, OVRTracker
from .tracking import load_trackers, start_recording, stop_recording, start_preview, stop_preview, init_handles
from .. import __package__ as base_package


class ToggleRecordOperator(bpy.types.Operator):
    bl_idname = "id.toggle_recording"
    bl_label = "Toggle OpenVR recording"

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext

        if not ovr_context.enabled:
            return {"FINISHED"}

        ovr_context.recording = not ovr_context.recording
        if ovr_context.recording:
            ovr_context.record_start_frame = context.scene.frame_current
            start_preview(ovr_context)
            start_recording()
        else:
            stop_recording(ovr_context)

        return {"FINISHED"}


class ToggleActiveOperator(bpy.types.Operator):
    bl_idname = "id.toggle_active"
    bl_label = "Toggle OpenVR's tracking state"

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext
        if ovr_context.enabled:
            stop_preview()
            openvr.shutdown()
            ovr_context.references_created = False
        else:
            openvr.init(openvr.VRApplication_Scene)

            manifest_path = Path(__file__).parent / "actions.json"
            vr_input = openvr.VRInput()

            set_manifest_fn = getattr(vr_input, "setActionManifestPath", None) or getattr(vr_input, "SetActionManifestPath", None)
            if not manifest_path.exists():
                print(f"[OpenVR] Action manifest not found: {manifest_path}")
            elif not set_manifest_fn:
                print("[OpenVR] VRInput has no setActionManifestPath method in this binding")
            else:
                try:
                    set_manifest_fn(str(manifest_path.resolve()))
                    print(f"[OpenVR] Set action manifest: {manifest_path}")
                except Exception as e:
                    print(f"[OpenVR] Failed to set action manifest: {e}")

            init_handles()
            load_trackers(ovr_context)
            start_preview(ovr_context)
            ovr_context.references_created = False

        ovr_context.enabled = not ovr_context.enabled
        return {"FINISHED"}


class CreateRefsOperator(bpy.types.Operator):
    bl_idname = "id.add_tracker_res"
    bl_label = "Create tracker target references"
    bl_options = {"UNDO"}

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext
        if not ovr_context.enabled:
            self.report({"ERROR"}, "OpenVR has not been connected yet")
            return {"FINISHED"}

        prev_obj = bpy.context.object
        if prev_obj:
            prev_mode = prev_obj.mode
            if prev_mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        else:
            prev_mode = "OBJECT"

        def _delete_with_descendants(obj: bpy.types.Object):
            to_remove = []

            def collect(target):
                to_remove.append(target)
                for child in target.children:
                    collect(child)

            collect(obj)
            for item in reversed(to_remove):
                if item.name in bpy.data.objects:
                    bpy.data.objects.remove(item)

        existing_roots = [obj for obj in bpy.data.objects if obj.name == "OVR Root" or obj.name.startswith("OVR Root.")]
        for existing_root in existing_roots:
            _delete_with_descendants(existing_root)

        bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 0))
        root_empty = bpy.context.object
        root_empty.name = "OVR Root"
        root_empty.empty_display_size = 0.1

        preferences: Preferences | None = context.preferences.addons[base_package].preferences
        install_dir = Path(preferences.steamvr_installation_path)

        system = openvr.VRSystem()

        fallback_models = {
            str(openvr.TrackedDeviceClass_GenericTracker): "vr_tracker_vive_3_0",
            str(openvr.TrackedDeviceClass_Controller): "vr_controller_vive_1_5",
            str(openvr.TrackedDeviceClass_HMD): "generic_hmd",
            str(openvr.TrackedDeviceClass_TrackingReference): "lh_basestation_valve_gen2",
        }

        model_templates: dict[str, bpy.types.Object] = {}

        model_db = {
            "vr_controller_knuckles_left": [
                install_dir / "drivers" / "indexcontroller" / "resources" / "rendermodels" / "valve_controller_knu_ev2_0_left" / "valve_controller_knu_ev2_0_left.obj",
            ],
            "vr_controller_knuckles_right": [
                install_dir / "drivers" / "indexcontroller" / "resources" / "rendermodels" / "valve_controller_knu_ev2_0_right" / "valve_controller_knu_ev2_0_right.obj",
            ],
            "pico_controller_left": [
                install_dir / "drivers" / "vrlink" / "resources" / "rendermodels" / "pico_4_controller_left" / "pico_4_controller_left.obj",
            ],
            "pico_controller_right": [
                install_dir / "drivers" / "vrlink" / "resources" / "rendermodels" / "pico_4_controller_right" / "pico_4_controller_right.obj",
            ],
            "tundra_tracker": [
                install_dir / "drivers" / "tundra_labs" / "resources" / "rendermodels" / "tundra_tracker" / "tundra_tracker.obj",
            ],
            "lh_basestation_valve_gen2": [
                install_dir / "resources" / "rendermodels" / "lh_basestation_valve_gen2" / "lh_basestation_valve_gen2.obj",
            ],
            "lh_basestation_vive": [
                install_dir / "resources" / "rendermodels" / "lh_basestation_vive" / "lh_basestation_vive.obj",
            ],
            "vr_tracker_vive_3_0": [
                install_dir / "drivers" / "htc" / "resources" / "rendermodels" / "vr_tracker_vive_3_0" / "vr_tracker_vive_3_0.obj",
                install_dir / "resources" / "rendermodels" / "vr_tracker_vive_3_0" / "vr_tracker_vive_3_0.obj",
            ],
            "vr_controller_vive_1_5": [
                install_dir / "resources" / "rendermodels" / "vr_controller_vive_1_5" / "vr_controller_vive_1_5.obj",
            ],
            "generic_hmd": [
                install_dir / "resources" / "rendermodels" / "generic_hmd" / "generic_hmd.obj",
            ],
        }

        def _get_prop(index: int, prop: int) -> str:
            try:
                return system.getStringTrackedDeviceProperty(index, prop)
            except Exception:
                return ""

        def _get_prop_int(index: int, prop: int) -> int | None:
            try:
                return system.getInt32TrackedDeviceProperty(index, prop)
            except Exception:
                return None

        def _controller_hand_side(tracker: OVRTracker) -> str | None:
            role = _get_prop_int(tracker.index, openvr.Prop_ControllerRoleHint_Int32)
            left_role = getattr(openvr, "TrackedControllerRole_LeftHand", 1)
            right_role = getattr(openvr, "TrackedControllerRole_RightHand", 2)
            if role == left_role:
                return "left"
            if role == right_role:
                return "right"
            return None

        def _resolve_model_obj_path(tracker: OVRTracker) -> Path | None:
            manufacturer = _get_prop(tracker.index, openvr.Prop_ManufacturerName_String).lower()
            model_number = _get_prop(tracker.index, openvr.Prop_ModelNumber_String).lower()
            controller_type = _get_prop(tracker.index, openvr.Prop_ControllerType_String).lower()

            keys = []
            controller_side = _controller_hand_side(tracker)

            if "index" in model_number or "knuckles" in controller_type or "knuckles" in model_number:
                if controller_side:
                    keys.append(f"vr_controller_knuckles_{controller_side}")

            if "pico" in manufacturer or "pico" in model_number:
                if controller_side:
                    keys.append(f"pico_controller_{controller_side}")

            if "tundra" in manufacturer or "tundra" in model_number:
                keys.append("tundra_tracker")

            if tracker.type == str(openvr.TrackedDeviceClass_TrackingReference):
                keys.extend(["lh_basestation_valve_gen2", "lh_basestation_vive"])

            class_model = fallback_models.get(tracker.type)
            if class_model:
                keys.append(class_model)

            for key in dict.fromkeys(keys):
                for model_path in model_db.get(key, []):
                    if model_path.exists():
                        return model_path

            return None

        def _get_or_import_model(path: Path) -> list[bpy.types.Object] | None:
            key = str(path.resolve())
            if key in model_templates:
                return model_templates[key]

            before = set(bpy.data.objects)
            try:
                bpy.ops.wm.obj_import(filepath=str(path))
            except RuntimeError:
                return None

            imported_objects = [obj for obj in bpy.data.objects if obj not in before]
            if not imported_objects:
                return None

            imported_set = set(imported_objects)
            root_objects = [obj for obj in imported_objects if obj.parent not in imported_set]
            if not root_objects:
                root_objects = imported_objects

            for imported in root_objects:
                imported.location = (0, 0, 0)
                imported.rotation_euler = (0, 0, 0)
                imported.scale = (1, 1, 1)

            model_templates[key] = root_objects
            return root_objects

        load_trackers(ovr_context)

        def select_model(target_model: bpy.types.Object):
            bpy.ops.object.select_all(action="DESELECT")
            target_model.select_set(True)
            bpy.context.view_layer.objects.active = target_model


        for tracker in ovr_context.trackers:
            tracker_name = tracker.name
            print(">", tracker_name)

            model_path = _resolve_model_obj_path(tracker)
            model_roots = _get_or_import_model(model_path) if model_path else None
            if not model_roots:
                print(f"Could not resolve model for {tracker_name}; skipping")
                continue

            tracker_target = bpy.data.objects.get(tracker_name)
            if tracker_target:
                _delete_with_descendants(tracker_target)

            bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 0))
            tracker_target = bpy.context.object
            tracker_target.name = tracker_name
            tracker_target.empty_display_size = 0.05
            tracker_target.show_name = True
            tracker_target.hide_render = True
            tracker_target.rotation_mode = "QUATERNION"
            tracker_target.parent = root_empty

            duplicated_roots = []
            for model_root in model_roots:
                select_model(model_root)
                bpy.ops.object.duplicate()
                dup_root = bpy.context.object
                duplicated_roots.append(dup_root)

            for i, dup_root in enumerate(duplicated_roots):
                dup_root.parent = tracker_target
                if i == 0:
                    dup_root.name = f"{tracker_name} Visual"

            tracker.target.object = tracker_target

        for template_roots in model_templates.values():
            for template_obj in template_roots:
                if template_obj and template_obj.name in bpy.data.objects:
                    _delete_with_descendants(template_obj)

        try:
            if prev_obj:
                prev_obj.select_set(True)
                bpy.context.view_layer.objects.active = prev_obj
                if bpy.context.object.mode != prev_mode:
                    bpy.ops.object.mode_set(mode=prev_mode)
        except ReferenceError:
            pass

        ovr_context.references_created = True
        print("Done")
        return {"FINISHED"}
