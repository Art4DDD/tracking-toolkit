import bpy
from bl_ui.space_view3d_toolbar import View3DPanel

from .operators import (
    ToggleActiveOperator,
    CreateRefsOperator,
    ToggleRecordOperator,
    ConvertSubframesOperator,
)
from .properties import OVRContext


class PANEL_UL_TrackerList(bpy.types.UIList):
    def draw_item(
            self,
            context,
            layout,
            data,
            item,
            icon,
            active_data,
            active_property,
            index,
            flt_flag,
    ):
        selected_tracker = item
        layout.prop(selected_tracker, "name", text="", emboss=False, icon_value=icon)


class RecorderPanel(View3DPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_openvr_recorder_menu"
    bl_label = "Tracking Toolkit Recorder"
    bl_category = "Track TK"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        ovr_context: OVRContext = context.scene.OVRContext

        layout.label(text="Tracking Toolkit Recorder")

        activate_label = "Disconnect/Reset OpenVR" if ovr_context.enabled else "Start/Connect OpenVR"
        layout.operator(ToggleActiveOperator.bl_idname, text=activate_label)

        layout.label(text="Manage Trackers")
        layout.template_list(
            "PANEL_UL_TrackerList",
            "",
            ovr_context,
            "trackers",
            ovr_context,
            "selected_tracker",
            rows=max(len(ovr_context.trackers), 1),
            type="DEFAULT"
        )

        layout.operator(CreateRefsOperator.bl_idname, text="Create References")

        root_obj = bpy.data.objects.get("OVR Root")
        has_references = ovr_context.references_created and bool(root_obj)

        if ovr_context.enabled and has_references:
            layout.label(text="Recording")

            record_btn_row = layout.row()
            record_btn_row.scale_y = 2
            record_btn_row.alert = ovr_context.recording

            active_record_label = "Stop Recording" if ovr_context.recording else "Start Recording"
            active_record_icon = "RECORD_ON" if ovr_context.recording else "RECORD_OFF"

            record_btn_row.operator(
                ToggleRecordOperator.bl_idname,
                text=active_record_label,
                icon=active_record_icon,
                depress=True,
            )

        show_tools = ovr_context.enabled or bool(root_obj)
        if not show_tools:
            return

        if ovr_context.recordings_made:
            convert_row = layout.row()
            convert_row.scale_y = 2
            convert_row.operator(
                ConvertSubframesOperator.bl_idname,
                text="Convert Subframes To Frames",
                icon="KEYTYPE_KEYFRAME_VEC",
            )
Subframes To Frames", icon="KEYTYPE_KEYFRAME_VEC")

        if not (ovr_context.references_ever_created and root_obj):
            return

        layout.label(text="Skeletal Fingers")

        def draw_hand_props(block_layout, title: str, prefix: str):
            box = block_layout.box()
            box.label(text=title)
            if not root_obj:
                box.label(text="OVR Root not found", icon="ERROR")
                return

            for suffix, label in (
                ("thumb_curl", "Thumb"),
                ("index_curl", "Index"),
                ("middle_curl", "Middle"),
                ("ring_curl", "Ring"),
                ("pinky_curl", "Pinky"),
            ):
                channel = f"{prefix}_{suffix}"
                if channel not in root_obj:
                    root_obj[channel] = 0.0
                box.prop(root_obj, f'["{channel}"]', text=label)

        draw_hand_props(layout, "Left Hand", "left")
        draw_hand_props(layout, "Right Hand", "right")
