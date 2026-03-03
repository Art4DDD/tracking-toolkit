import bpy
import openvr
import time
from mathutils import Matrix, Vector
from pathlib import Path

from .properties import Preferences, OVRContext, OVRTracker
from .tracking import _get_poses, load_trackers, start_recording, stop_recording, start_preview, stop_preview, init_handles
from .. import __package__ as base_package


TRACKER_BOX_DEFAULT_HALF_EXTENT = 0.025
TRACKER_BOX_MIN_AXIS_SIZE = 0.01
TRACKER_BOX_MIN_AXIS_PADDING = 0.005
TRACKER_BOX_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
]


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
                if item.get("_ovr_tracker_box"):
                    continue
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
                return Vector((-TRACKER_BOX_DEFAULT_HALF_EXTENT, -TRACKER_BOX_DEFAULT_HALF_EXTENT, -TRACKER_BOX_DEFAULT_HALF_EXTENT)), Vector((TRACKER_BOX_DEFAULT_HALF_EXTENT, TRACKER_BOX_DEFAULT_HALF_EXTENT, TRACKER_BOX_DEFAULT_HALF_EXTENT))

            min_corner = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
            max_corner = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))

            for axis in ("x", "y", "z"):
                axis_min = getattr(min_corner, axis)
                axis_max = getattr(max_corner, axis)
                if abs(axis_max - axis_min) < TRACKER_BOX_MIN_AXIS_SIZE:
                    setattr(min_corner, axis, axis_min - TRACKER_BOX_MIN_AXIS_PADDING)
                    setattr(max_corner, axis, axis_max + TRACKER_BOX_MIN_AXIS_PADDING)

            return min_corner, max_corner

        def _ensure_tracker_box(tracker_name: str) -> bpy.types.Object:
            tracker_obj = bpy.data.objects.get(tracker_name)
            if tracker_obj and tracker_obj.type == "MESH" and tracker_obj.get("_ovr_tracker_box"):
                return tracker_obj

            tracker_children = []
            old_matrix_world = Matrix.Identity(4)
            if tracker_obj:
                tracker_children = list(tracker_obj.children)
                old_matrix_world = tracker_obj.matrix_world.copy()
                bpy.data.objects.remove(tracker_obj)

            tracker_mesh = bpy.data.meshes.new(f"{tracker_name} Mesh")
            tracker_obj = bpy.data.objects.new(tracker_name, tracker_mesh)
            context.collection.objects.link(tracker_obj)
            tracker_obj.matrix_world = old_matrix_world
            tracker_obj.show_name = True
            tracker_obj.hide_render = True
            tracker_obj.hide_select = False
            tracker_obj.display_type = "WIRE"
            tracker_obj.show_wire = True
            tracker_obj.show_all_edges = True
            tracker_obj.show_in_front = True
            tracker_obj.rotation_mode = "QUATERNION"
            tracker_obj["_ovr_tracker_box"] = True

            for child in tracker_children:
                child.parent = tracker_obj

            return tracker_obj

        def _fit_tracker_box(tracker_obj: bpy.types.Object, roots: list[bpy.types.Object]):
            min_corner, max_corner = _compute_local_bounds(roots, tracker_obj)

            verts = [
                (min_corner.x, min_corner.y, min_corner.z),
                (max_corner.x, min_corner.y, min_corner.z),
                (max_corner.x, max_corner.y, min_corner.z),
                (min_corner.x, max_corner.y, min_corner.z),
                (min_corner.x, min_corner.y, max_corner.z),
                (max_corner.x, min_corner.y, max_corner.z),
                (max_corner.x, max_corner.y, max_corner.z),
                (min_corner.x, max_corner.y, max_corner.z),
            ]
            tracker_mesh = tracker_obj.data
            tracker_mesh.clear_geometry()
            tracker_mesh.from_pydata(verts, TRACKER_BOX_EDGES, [])
            tracker_mesh.update()

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

        model_templates: dict[str, list[bpy.types.Object]] = {}
        vr_render_models = openvr.VRRenderModels()

        def _normalize_render_model_name(name: str) -> str:
            return (name or "").strip().replace("\\", "/").split("/")[-1]

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

        def _resolve_model_name(tracker: OVRTracker) -> str | None:
            manufacturer = _get_prop(tracker.index, openvr.Prop_ManufacturerName_String).lower()
            model_number = _get_prop(tracker.index, openvr.Prop_ModelNumber_String).lower()
            controller_type = _get_prop(tracker.index, openvr.Prop_ControllerType_String).lower()
            render_model_name = _normalize_render_model_name(_get_prop(tracker.index, openvr.Prop_RenderModelName_String))

            keys: list[str] = []
            controller_side = _controller_hand_side(tracker)

            if render_model_name:
                keys.extend([render_model_name, render_model_name.lower()])

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
            position = getattr(vertex, "vPosition", None)
            if hasattr(position, "v"):
                return float(position.v[0]), float(position.v[1]), float(position.v[2])
            if isinstance(position, (tuple, list)) and len(position) >= 3:
                return float(position[0]), float(position[1]), float(position[2])
            return 0.0, 0.0, 0.0

        def _load_render_model(model_name: str) -> list[bpy.types.Object] | None:
            loading_error = getattr(openvr, "VRRenderModelError_Loading", 100)
            ok_error = getattr(openvr, "VRRenderModelError_None", 0)

            model = None
            for _ in range(5000):
                error, model = vr_render_models.loadRenderModel_Async(model_name)

                if error == loading_error:
                    time.sleep(0.001)
                    continue

                if error != ok_error:
                    return None

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

                obj = bpy.data.objects.new(f"{model_name} Template", mesh)
                context.scene.collection.objects.link(obj)
                obj.hide_render = True
                obj.hide_set(True)
                obj.hide_select = True
                obj.show_name = False
                return [obj]
            finally:
                try:
                    vr_render_models.freeRenderModel(model)
                except Exception:
                    pass

        def _get_or_import_model(model_name: str) -> list[bpy.types.Object] | None:
            if model_name in model_templates:
                return model_templates[model_name]

            model_roots = _load_render_model(model_name)
            if not model_roots:
                return None

            _set_disable_selection(model_roots)
            model_templates[model_name] = model_roots
            return model_roots

        def _duplicate_hierarchy(root_obj: bpy.types.Object, parent_obj: bpy.types.Object) -> bpy.types.Object:
            duplicated_by_source = {}

            objects_to_copy = list(_iter_hierarchy([root_obj]))
            for src in objects_to_copy:
                dup = src.copy()
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

        pose_by_index: dict[int, Matrix] = {}
        try:
            for _, pose_tracker, pose_matrix in _get_poses(ovr_context):
                pose_by_index[pose_tracker.index] = pose_matrix
        except Exception:
            pose_by_index = {}

        for tracker in ovr_context.trackers:
            tracker_name = tracker.name
            print(">", tracker_name)

            model_name = _resolve_model_name(tracker)
            model_roots = _get_or_import_model(model_name) if model_name else None
            if not model_roots:
                print(f"Could not resolve model for {tracker_name}; skipping")
                continue

            tracker_target = _ensure_tracker_box(tracker_name)
            tracker_target.parent = root_empty
            if tracker.index in pose_by_index:
                tracker_target.matrix_world = root_empty.matrix_world @ pose_by_index[tracker.index]

            existing_visual_children = [child for child in tracker_target.children]
            if existing_visual_children:
                context.view_layer.update()
                _fit_tracker_box(tracker_target, existing_visual_children)
                _set_disable_selection(existing_visual_children)
                tracker.target.object = tracker_target
                continue

            duplicated_roots = []
            for model_root in model_roots:
                dup_root = _duplicate_hierarchy(model_root, tracker_target)
                duplicated_roots.append(dup_root)

            for i, dup_root in enumerate(duplicated_roots):
                if i == 0:
                    dup_root.name = f"{tracker_name} Visual"

            _set_disable_selection(duplicated_roots)
            context.view_layer.update()
            _fit_tracker_box(tracker_target, duplicated_roots)
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
