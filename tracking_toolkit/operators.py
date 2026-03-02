import bpy
import openvr
from pathlib import Path
from mathutils import Vector

from .properties import Preferences, OVRTransform, OVRContext, OVRTracker
from .tracking import load_trackers, start_recording, stop_recording, start_preview, stop_preview, init_handles
from .. import __package__ as base_package


class BuildArmatureOperator(bpy.types.Operator):
    bl_idname = "id.build_armature"
    bl_label = "Build OpenVR armature"

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext

        # Store selection and mode state
        prev_select = None
        if context.object:
            prev_select = context.object

            prev_mode = context.object.mode
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                # Maybe a linked library or something
                pass
        else:
            prev_mode = "OBJECT"

        # Create armature
        armature_obj = context.scene.objects.get("OVR Armature")
        if not armature_obj:
            bpy.ops.object.armature_add(enter_editmode=True, align="WORLD", location=(0, 0, 0))
            armature_obj = context.object
            armature_obj.name = "OVR Armature"

        context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode="EDIT")

        armature_data = armature_obj.data
        armature_data.name = "OVR Armature Data"
        armature_data.show_names = True
        armature_data.display_type = "STICK"

        edit_bones = armature_data.edit_bones
        # Clear any default bone Blender might have added, or all bones if recreating
        for bone in list(edit_bones):  # Iterate over a copy as we are modifying the list
            edit_bones.remove(bone)

        def get_loc(obj_prop) -> Vector | None:
            if obj_prop:
                return obj_prop.matrix_world.translation.copy()  # Use a copy to prevent changing it
            return None

        joints = ovr_context.armature_joints

        if not joints.hips:
            self.report(
                {"ERROR"},
                "Hips are required to build an armature."
            )
            return {"CANCELLED"}

        if joints.l_foot and joints.r_foot:
            foot_height = (get_loc(joints.l_foot).z + get_loc(joints.r_foot).z) / 2  # Average of feet
        else:
            foot_height = 0

        float_height = foot_height

        # Offset floor
        foot_height = 0

        hips_height = get_loc(joints.hips).z - float_height
        head_height = (get_loc(joints.head).z - float_height
                       if joints.head
                       else hips_height * 1.8)  # Default height (assume the tracker is higher up on waist)

        chest_height = (get_loc(joints.chest).z - float_height
                        if joints.chest
                        else (hips_height + head_height) / 2)  # If no chest, average hips and head
        knee_height = ((get_loc(joints.l_knee).z + get_loc(joints.r_knee).z) / 2 - float_height
                       if joints.l_knee and joints.r_knee
                       else (foot_height + hips_height) / 2)  # If no knees, average hips and feet

        neck_height = head_height - head_height / 8  # Person is 8 heads tall about

        # Skip elbow height since we are t-posing

        head_loc = Vector((0, 0, head_height))
        hips_loc = Vector((0, 0, hips_height))
        chest_loc = Vector((0, 0, chest_height))
        neck_loc = Vector((0, 0, neck_height))
        root_loc = Vector((0, 0, hips_height - 0.01))

        l_foot_loc = Vector((0.15, 0, foot_height))
        r_foot_loc = Vector((-0.15, 0, foot_height))

        half_height = head_height / 2  # Half of wingspan

        # Offset 0.1 since it's added back later for hand tips
        l_hand_loc = Vector((half_height, 0, chest_height))
        r_hand_loc = Vector((-half_height, 0, chest_height))

        elbow_offset = half_height / 2  # Elbows halfway between hands
        elbow_height = (chest_height + neck_height) / 2

        l_elbow_loc = Vector((elbow_offset, 0, elbow_height))
        r_elbow_loc = Vector((-elbow_offset, 0, elbow_height))

        l_knee_loc = Vector((0.15, -0.2, knee_height))
        r_knee_loc = Vector((-0.15, -0.2, knee_height))

        # Name, parent name, parent_obj, head_loc, tail_loc, head_obj
        bone_definitions = [
            ("root", None, joints.hips, root_loc, hips_loc),  # Root to hips
            ("spine", "root", joints.head, hips_loc, neck_loc),  # Hips to neck
            ("head", "spine", joints.head, chest_loc, head_loc),  # Chest to head

            # Arms and hands
            ("arm.l", "spine", joints.l_elbow or joints.l_hand, chest_loc, l_elbow_loc),  # Chest to elbow
            ("arm.r", "spine", joints.r_elbow or joints.r_hand, chest_loc, r_elbow_loc),  # Chest to elbow

            ("forearm.l", "arm.l", joints.l_hand, l_elbow_loc, l_hand_loc),  # Elbow to hand
            ("forearm.r", "arm.r", joints.r_hand, r_elbow_loc, r_hand_loc),  # Elbow to hand

            ("hand.l", "forearm.l", joints.l_hand, l_hand_loc, l_hand_loc + Vector((0.1, 0, 0))),  # Hand to hand tip
            ("hand.r", "forearm.r", joints.r_hand, r_hand_loc, r_hand_loc + Vector((-0.1, 0, 0))),  # Hand to hand tip

            # Legs and feet
            ("thigh.l", "root", joints.l_knee or joints.l_foot, hips_loc, l_knee_loc),  # Hips to knee
            ("thigh.r", "root", joints.r_knee or joints.r_foot, hips_loc, r_knee_loc),  # Hips to knee

            ("leg.l", "thigh.l", joints.l_foot, l_knee_loc, l_foot_loc),  # L foot to elbow
            ("leg.r", "thigh.r", joints.r_foot, r_knee_loc, r_foot_loc),  # R foot to elbo

            ("foot.l", "leg.l", joints.l_foot, l_foot_loc, l_foot_loc + Vector((0, -0.2, 0))),
            ("foot.r", "leg.r", joints.r_foot, r_foot_loc, r_foot_loc + Vector((0, -0.2, 0))),
        ]

        bones = {}

        for name, parent_name, _, head_loc, tail_loc in bone_definitions:
            bone = edit_bones.new(name)

            bone.head = head_loc
            bone.tail = tail_loc

            bones[name] = bone

            if parent_name:
                bone.parent = bones[parent_name]
                bone.use_connect = True

        # Add constraints
        bpy.ops.object.mode_set(mode="POSE")

        # FK for limbs and spine
        locked_track_bones = [
            "spine",
            "thigh.l",
            "thigh.r"
        ]

        damped_track_bones = []
        if joints.l_elbow:
            damped_track_bones.append("arm.l")
        if joints.r_elbow:
            damped_track_bones.append("arm.r")

        # Actually add
        for name, _, parent_obj, _, _ in bone_definitions:
            if parent_obj:
                pose_bone: bpy.types.PoseBone = armature_obj.pose.bones.get(name)
                if not pose_bone:
                    print(f"No bone for {name}")
                    continue

                # Clear existing
                for constraint in pose_bone.constraints:
                    pose_bone.constraints.remove(constraint)

                if name in ["root", "head", "hand.l", "hand.r", "foot.r", "foot.l"]:
                    # Location and rotation (no scale because it gets weird)
                    constraint_loc = pose_bone.constraints.new("COPY_LOCATION")
                    constraint_loc.name = "Tracker Binding Location"
                    constraint_loc.target = parent_obj

                    constraint_rot = pose_bone.constraints.new("COPY_ROTATION")
                    constraint_rot.name = "Tracker Binding Rotation"
                    constraint_rot.target = parent_obj

                if name in ["hand.l", "hand.r", "foot.r", "foot.l"]:
                    constraint = pose_bone.constraints.new("IK")
                    constraint.name = "Tracker Binding Child"
                    constraint.target = parent_obj
                    constraint.chain_count = 2
                    constraint.use_tail = False

                if name in damped_track_bones:
                    constraint = pose_bone.constraints.new("DAMPED_TRACK")
                    constraint.name = "Tracker Binding Track"
                    constraint.target = parent_obj

                if name in locked_track_bones:
                    constraint = pose_bone.constraints.new("LOCKED_TRACK")
                    constraint.lock_axis = "LOCK_X"
                    constraint.name = "Tracker Binding Track"
                    constraint.target = parent_obj

        # Restore mode
        if prev_select:
            context.view_layer.objects.active = prev_select
            prev_select.select_set(True)

        try:
            bpy.ops.object.mode_set(mode=prev_mode)
        except RuntimeError:
            # Maybe a linked library or something
            pass

        return {"FINISHED"}


class ToggleRecordOperator(bpy.types.Operator):
    bl_idname = "id.toggle_recording"
    bl_label = "Toggle OpenVR recording"

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext

        # Double check state, though this should have been checked before
        if not ovr_context.enabled:
            return {"FINISHED"}

        if ovr_context.calibration_stage != 0:
            return {"FINISHED"}

        ovr_context.recording = not ovr_context.recording
        if ovr_context.recording:
            ovr_context.record_start_frame = context.scene.frame_current
            start_preview(ovr_context)
            start_recording()
        else:
            stop_recording(ovr_context)

        return {"FINISHED"}


class ToggleCalibrationOperator(bpy.types.Operator):
    bl_idname = "id.toggle_calibration"
    bl_label = "Toggle OpenVR calibration"

    @staticmethod
    def obj_t_to_prop(obj: bpy.types.Object, prop: OVRTransform):
        prop.location = obj.location
        prop.rotation = obj.rotation_euler
        # Scale shouldn't be applicable to calibration, and OpenVR will sometimes provide non-1 scale factors.
        # Just keep it as is, since some armatures depend on scale and it causes issues.

    @staticmethod
    def prop_t_to_obj(prop: OVRTransform, obj: bpy.types.Object):
        obj.location = prop.location
        obj.rotation_euler = prop.rotation
        # Same as above, skip scale.

    def restore_calibration_transforms(self, ovr_context):
        # Restore transform of trackers
        for tracker in ovr_context.trackers:
            if not tracker.connected:
                continue

            tracker_name = tracker.name
            tracker_obj = bpy.data.objects.get(tracker_name)
            if not tracker_obj:
                continue

            # Save original transforms
            self.obj_t_to_prop(tracker_obj, tracker.target.transform)

            # Restore calibration transforms
            self.prop_t_to_obj(tracker.target.calibration_transform, tracker_obj)

    def save_calibration_transforms(self, ovr_context):
        # Save transform of trackers
        for tracker in ovr_context.trackers:
            if not tracker.connected:
                continue

            tracker_name = tracker.name
            tracker_obj = bpy.data.objects.get(tracker_name)
            if not tracker_obj:
                continue

            # Save calibration transforms
            self.obj_t_to_prop(tracker_obj, tracker.target.calibration_transform)

            # Restore original transforms
            self.prop_t_to_obj(tracker.target.transform, tracker_obj)

    @staticmethod
    def enable_rest():
        # Put all armatures in rest position
        for obj in bpy.context.scene.objects:
            if obj.type == "ARMATURE":
                obj.data.pose_position = "REST"
        return {"FINISHED"}

    @staticmethod
    def disable_rest():
        # Put all armatures in rest position
        for obj in bpy.context.scene.objects:
            if obj.type == "ARMATURE":
                obj.data.pose_position = "POSE"
        return {"FINISHED"}

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext

        # Next stage
        ovr_context.calibration_stage += 1
        if ovr_context.calibration_stage > 2:
            ovr_context.calibration_stage = 0

        # Cycle through stages
        if ovr_context.calibration_stage == 0:  # Complete calibration
            self.save_calibration_transforms(ovr_context)
            self.disable_rest()
            start_preview(ovr_context)
        elif ovr_context.calibration_stage == 1:  # Tracker Alignment
            stop_preview()
            self.restore_calibration_transforms(ovr_context)
            self.enable_rest()
        elif ovr_context.calibration_stage == 2:  # Tracker Offsetting
            stop_preview()
            self.disable_rest()

        return {"FINISHED"}


class ToggleActiveOperator(bpy.types.Operator):
    bl_idname = "id.toggle_active"
    bl_label = "Toggle OpenVR's tracking state"

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext
        if ovr_context.enabled:
            stop_preview()
            openvr.shutdown()
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

        ovr_context.enabled = not ovr_context.enabled
        return {"FINISHED"}


class CreateRefsOperator(bpy.types.Operator):
    bl_idname = "id.add_tracker_res"
    bl_label = "Create tracker target references"
    bl_options = {"UNDO"}

    def execute(self, context):
        ovr_context: OVRContext = context.scene.OVRContext
        if not ovr_context.enabled:
            self.report(
                {"ERROR"},
                "OpenVR has not been connected yet"
            )
            return {"FINISHED"}

        # Set to object mode while keeping track of the previous one
        prev_obj = bpy.context.object
        if prev_obj:
            prev_mode = prev_obj.mode
            if prev_mode != "OBJECT":  # Safe against linked library immutability
                bpy.ops.object.mode_set(mode="OBJECT")

        # Create root
        root_empty = bpy.data.objects.get("OVR Root")
        if root_empty:
            bpy.data.objects.remove(root_empty)

        bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 0))
        root_empty = bpy.context.object
        root_empty.name = "OVR Root"
        root_empty.empty_display_size = 0.1

        # Import models

        # Get model paths from preferences
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

        # Static model database (explicit known model paths, no directory scanning)
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

        def _resolve_openvr_model_obj_path(tracker: OVRTracker) -> Path | None:
            render_model_name = _get_prop(tracker.index, openvr.Prop_RenderModelName_String)
            resource_root = _get_prop(tracker.index, openvr.Prop_ResourceRoot_String)
            if not render_model_name or not resource_root:
                return None

            root_path = Path(resource_root)
            model_path = root_path / "rendermodels" / render_model_name / f"{render_model_name}.obj"
            if model_path.exists():
                return model_path
            return None

        def _resolve_model_obj_path(tracker: OVRTracker) -> Path | None:
            direct_openvr_model = _resolve_openvr_model_obj_path(tracker)
            if direct_openvr_model:
                return direct_openvr_model

            render_model_name = _get_prop(tracker.index, openvr.Prop_RenderModelName_String).lower()
            manufacturer = _get_prop(tracker.index, openvr.Prop_ManufacturerName_String).lower()
            model_number = _get_prop(tracker.index, openvr.Prop_ModelNumber_String).lower()
            controller_type = _get_prop(tracker.index, openvr.Prop_ControllerType_String).lower()

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
                for model_path in model_db.get(key, []):
                    if model_path.exists():
                        return model_path

            return None

        def _get_or_import_model(path: Path) -> bpy.types.Object | None:
            key = str(path.resolve())
            cached = model_templates.get(key)
            if cached and cached.name in bpy.data.objects:
                return cached

            try:
                bpy.ops.wm.obj_import(filepath=str(path))
            except RuntimeError:
                return None

            imported = bpy.context.object
            if not imported:
                return None

            imported.location = (0, 0, 0)
            imported.rotation_euler = (0, 0, 0)
            imported.scale = (1, 1, 1)
            imported.name = f"TTK_Template_{path.stem}"
            imported.hide_render = True

            model_templates[key] = imported
            return imported

        load_trackers(ovr_context)

        # Create references
        def select_model(target_model: bpy.types.Object):
            bpy.ops.object.select_all(action="DESELECT")
            target_model.select_set(True)
            bpy.context.view_layer.objects.active = target_model

        for tracker in ovr_context.trackers:
            # Create new tracker empty if it doesn't exist
            tracker_name = tracker.name

            print(">", tracker_name)

            model_path = _resolve_model_obj_path(tracker)
            model = _get_or_import_model(model_path) if model_path else None

            if not model:
                print(f"Could not resolve model for {tracker_name}; skipping")
                continue

            # Delete existing target
            tracker_target = bpy.data.objects.get(tracker_name)
            if tracker_target and tracker_target != model:
                bpy.data.objects.remove(tracker_target)

            # Create target
            select_model(model)
            bpy.ops.object.duplicate()

            tracker_target = bpy.context.object
            tracker_target.name = tracker_name

            tracker_target.show_name = True
            tracker_target.hide_render = True

            # Create another empty as a joint offset. This is useful when you use a "Copy Transforms" constraint but
            # the physical tracker doesn't align perfectly with a character's joint
            # Create one if it doesn't exist
            joint_name = f"{tracker_name} Joint"

            # Delete existing joint
            tracker_joint = bpy.data.objects.get(joint_name)
            if tracker_joint and tracker_joint != model:
                bpy.data.objects.remove(tracker_joint)

            # Create joint
            select_model(model)
            bpy.ops.object.duplicate()

            tracker_joint = bpy.context.object
            tracker_joint.name = joint_name

            tracker_joint.display_type = "WIRE"
            tracker_joint.show_in_front = True
            tracker_target.hide_render = True

            # Assign objects
            tracker.target.object = tracker_target
            tracker.joint.object = tracker_joint

            # Set up parenting
            tracker_target.parent = root_empty
            tracker_joint.parent = tracker_target

            # Set up rotation modes
            tracker_target.rotation_mode = "QUATERNION"
            tracker_joint.rotation_mode = "QUATERNION"

        # Clean up imported template models
        for template_obj in model_templates.values():
            if template_obj and template_obj.name in bpy.data.objects:
                bpy.data.objects.remove(template_obj)

        # Restore previous selection
        try:
            if prev_obj:
                prev_obj.select_set(True)
                bpy.context.view_layer.objects.active = prev_obj

                # I can't stand warnings, okay?
                # noinspection PyUnboundLocalVariable
                if bpy.context.object.mode != prev_mode:  # Safe against linked library immutability
                    bpy.ops.object.mode_set(mode=prev_mode)
        except ReferenceError:
            pass

        print("Done")
        return {"FINISHED"}
