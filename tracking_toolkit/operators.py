import bpy
import openvr
import time
from mathutils import Vector
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

        if ovr_context.recording:
            self.report({"ERROR"}, "Stop recording before creating references")
            return {"FINISHED"}

        stop_preview()

        prev_obj = bpy.context.object
        if prev_obj:
            prev_mode = prev_obj.mode
            if prev_mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        else:
            prev_mode = "OBJECT"

        def _iter_hierarchy(roots: list[bpy.types.Object]):
            seen = set()
            stack = list(roots)
            while stack:
                item = stack.pop()
                if item in seen:
                    continue
                seen.add(item)
                yield item
                stack.extend(item.children)

        def _set_disable_selection(roots: list[bpy.types.Object]):
            for item in _iter_hierarchy(roots):
                item.hide_select = True

        def _delete_with_descendants(roots: list[bpy.types.Object]):
            items = list(_iter_hierarchy(roots))
            for item in reversed(items):
                if item and item.name in bpy.data.objects:
                    bpy.data.objects.remove(item)

        def _compute_local_bounds(roots: list[bpy.types.Object], parent_obj: bpy.types.Object):
            points = []
            parent_inv = parent_obj.matrix_world.inverted()

            for item in _iter_hierarchy(roots):
                if item.type != "MESH":
                    continue
                for corner in item.bound_box:
                    world_corner = item.matrix_world @ Vector(corner)
                    local_corner = parent_inv @ world_corner
                    points.append(local_corner)

            if not points:
                return Vector((0.0, 0.0, 0.0)), Vector((0.05, 0.05, 0.05))

            min_corner = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
            max_corner = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
            center = (min_corner + max_corner) * 0.5
            dimensions = max_corner - min_corner
            dimensions.x = max(dimensions.x, 0.01)
            dimensions.y = max(dimensions.y, 0.01)
            dimensions.z = max(dimensions.z, 0.01)
            return center, dimensions

        def _ensure_bounds_empty(parent_obj: bpy.types.Object) -> bpy.types.Object:
            bounds_name = f"{parent_obj.name} Bounds"
            bounds_obj = bpy.data.objects.get(bounds_name)
            if bounds_obj and bounds_obj.parent == parent_obj:
                return bounds_obj

            bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 0))
            bounds_obj = bpy.context.object
            bounds_obj.name = bounds_name
            bounds_obj.parent = parent_obj
            bounds_obj.hide_select = True
            bounds_obj.hide_render = True
            bounds_obj.show_name = False
            bounds_obj.empty_display_size = 1.0
            return bounds_obj

        def _fit_bounds_empty(parent_obj: bpy.types.Object, roots: list[bpy.types.Object]):
            center, dimensions = _compute_local_bounds(roots, parent_obj)
            bounds_obj = _ensure_bounds_empty(parent_obj)
            bounds_obj.location = center
            bounds_obj.scale = (max(dimensions.x * 0.5, 0.005), max(dimensions.y * 0.5, 0.005), max(dimensions.z * 0.5, 0.005))

        root_empty = bpy.data.objects.get("OVR Root")
        if not root_empty:
            bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 0))
            root_empty = bpy.context.object
            root_empty.name = "OVR Root"
            root_empty.empty_display_size = 0.1

        _preferences: Preferences | None = context.preferences.addons[base_package].preferences

        system = openvr.VRSystem()

        fallback_models = {
            str(openvr.TrackedDeviceClass_GenericTracker): "vr_tracker_vive_3_0",
            str(openvr.TrackedDeviceClass_Controller): "vr_controller_vive_1_5",
            str(openvr.TrackedDeviceClass_HMD): "generic_hmd",
            str(openvr.TrackedDeviceClass_TrackingReference): "lh_basestation_valve_gen2",
        }

        model_templates: dict[str, bpy.types.Object] = {}

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

        vr_render_models = openvr.VRRenderModels()

        def _normalize_render_model_name(name: str) -> str:
            return (name or "").strip().replace("\\", "/").split("/")[-1]

        def _resolve_model_name(tracker: OVRTracker) -> str | None:
            manufacturer = _get_prop(tracker.index, openvr.Prop_ManufacturerName_String).lower()
            model_number = _get_prop(tracker.index, openvr.Prop_ModelNumber_String).lower()
            controller_type = _get_prop(tracker.index, openvr.Prop_ControllerType_String).lower()
            render_model_name = _normalize_render_model_name(_get_prop(tracker.index, openvr.Prop_RenderModelName_String))

            keys = []
            controller_side = _controller_hand_side(tracker)

            if render_model_name:
                keys.append(render_model_name)

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
                if key:
                    return key
            return None

        def _extract_vertex_position(vertex) -> tuple[float, float, float]:
            pos = getattr(vertex, "vPosition", None)
            if hasattr(pos, "v"):
                return float(pos.v[0]), float(pos.v[1]), float(pos.v[2])
            if isinstance(pos, (tuple, list)) and len(pos) >= 3:
                return float(pos[0]), float(pos[1]), float(pos[2])
            return 0.0, 0.0, 0.0

        def _load_render_model_mesh(model_name: str) -> bpy.types.Mesh | None:
            model = None
            for _ in range(5000):
                try:
                    result = vr_render_models.loadRenderModel_Async(model_name)
                except Exception as exc:
                    if exc.__class__.__name__ == "RenderModelError_Loading":
                        time.sleep(0.001)
                        continue
                    return None

                if isinstance(result, tuple):
                    if len(result) >= 2:
                        error, model = result[0], result[1]
                        loading_error = getattr(openvr, "VRRenderModelError_Loading", 100)
                        ok_error = getattr(openvr, "VRRenderModelError_None", 0)
                        if error == loading_error:
                            time.sleep(0.001)
                            continue
                        if error != ok_error:
                            return None
                    elif len(result) == 1:
                        model = result[0]
                else:
                    model = result

                if model is not None:
                    break

            if model is None:
                return None

            try:
                vertex_count = int(getattr(model, "unVertexCount", 0))
                triangle_count = int(getattr(model, "unTriangleCount", 0))
                if vertex_count <= 0 or triangle_count <= 0:
                    return None

                vertices = [_extract_vertex_position(model.rVertexData[i]) for i in range(vertex_count)]
                index_count = triangle_count * 3
                indices = [int(model.rIndexData[i]) for i in range(index_count)]
                faces = [tuple(indices[i:i + 3]) for i in range(0, index_count, 3)]

                mesh = bpy.data.meshes.new(f"{model_name} Template Mesh")
                mesh.from_pydata(vertices, [], faces)
                mesh.update()
                return mesh
            finally:
                try:
                    vr_render_models.freeRenderModel(model)
                except Exception:
                    pass

        def _get_or_import_model(model_name: str) -> list[bpy.types.Object] | None:
            if model_name in model_templates:
                return model_templates[model_name]

            mesh = _load_render_model_mesh(model_name)
            if not mesh:
                return None

            obj = bpy.data.objects.new(f"{model_name} Template", mesh)
            context.scene.collection.objects.link(obj)
            obj.location = (0, 0, 0)
            obj.rotation_euler = (0, 0, 0)
            obj.scale = (1, 1, 1)
            obj.hide_render = True
            obj.show_name = False
            _set_disable_selection([obj])
            model_templates[model_name] = [obj]
            return [obj]

        def _duplicate_hierarchy(root_obj: bpy.types.Object, parent_obj: bpy.types.Object) -> bpy.types.Object:
            duplicated_by_source = {}

            objects_to_copy = list(_iter_hierarchy([root_obj]))
            for src in objects_to_copy:
                dup = src.copy()
                if src.data:
                    dup.data = src.data.copy()
                dup.parent = None
                context.scene.collection.objects.link(dup)
                duplicated_by_source[src] = dup

            for src in objects_to_copy:
                dup = duplicated_by_source[src]
                if src == root_obj:
                    dup.parent = parent_obj
                elif src.parent in duplicated_by_source:
                    dup.parent = duplicated_by_source[src.parent]

            return duplicated_by_source[root_obj]

        load_trackers(ovr_context)

        for tracker in ovr_context.trackers:
            tracker_name = tracker.name
            print(">", tracker_name)

            model_name = _resolve_model_name(tracker)
            model_roots = _get_or_import_model(model_name) if model_name else None
            if not model_roots:
                print(f"Could not resolve model for {tracker_name}; skipping")
                continue

            tracker_target = bpy.data.objects.get(tracker_name)
            if tracker_target:
                tracker_target.parent = root_empty
                visual_children = [child for child in tracker_target.children if not child.name.endswith(" Bounds")]
                if visual_children:
                    _fit_bounds_empty(tracker_target, visual_children)
                _set_disable_selection(list(tracker_target.children))
                tracker.target.object = tracker_target
                continue

            bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 0))
            tracker_target = bpy.context.object
            tracker_target.name = tracker_name
            tracker_target.empty_display_type = "PLAIN_AXES"
            tracker_target.empty_display_size = 0.02
            tracker_target.show_name = True
            tracker_target.hide_render = True
            tracker_target.rotation_mode = "QUATERNION"
            tracker_target.parent = root_empty

            duplicated_roots = []
            for model_root in model_roots:
                dup_root = _duplicate_hierarchy(model_root, tracker_target)
                duplicated_roots.append(dup_root)

            for i, dup_root in enumerate(duplicated_roots):
                if i == 0:
                    dup_root.name = f"{tracker_name} Visual"

            _set_disable_selection(duplicated_roots)
            _fit_bounds_empty(tracker_target, duplicated_roots)
            tracker.target.object = tracker_target

        for template_roots in model_templates.values():
            _delete_with_descendants(template_roots)

        try:
            if prev_obj:
                prev_obj.select_set(True)
                bpy.context.view_layer.objects.active = prev_obj
                if bpy.context.object.mode != prev_mode:
                    bpy.ops.object.mode_set(mode=prev_mode)
        except ReferenceError:
            pass

        ovr_context.references_created = True
        start_preview(ovr_context)
        print("Done")
        return {"FINISHED"}
