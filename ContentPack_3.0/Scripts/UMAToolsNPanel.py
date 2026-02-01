#!/usr/bin/env python3
# -*- coding: utf-8 -*-

bl_info = {
    "name": "UMA Tools",
    "author": "UMA Open Source",
    "version": (1, 0, 10),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > UMA Tools",
    "description": "Quick checks and fixes for UMA export readiness.",
    "category": "3D View",
}

import bpy
import os
from mathutils import Matrix


ADDON_VERSION_STR = "1.10"


def _uma_default_export_path(suffix):
    if bpy.data.filepath:
        blend_dir = os.path.dirname(bpy.data.filepath)
        blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
        return os.path.join(blend_dir, blend_name + suffix + ".fbx")

    tmp_dir = bpy.app.tempdir
    if tmp_dir:
        return os.path.join(tmp_dir, "uma" + suffix + ".fbx")

    return os.path.join(os.path.expanduser("~"), "uma" + suffix + ".fbx")


def _iter_target_objects(context, selected_only: bool):
    if selected_only:
        objs = list(context.selected_objects)
    else:
        objs = list(context.scene.objects)

    # Only process meshes and armatures (bones) for UMA workflows.
    return [o for o in objs if o is not None and o.type in {'MESH', 'ARMATURE'}]


def _needs_transform_apply(obj: bpy.types.Object) -> bool:
    # Match Blender UI meaning: apply if not default.
    if obj is None:
        return False

    # Ignore non-transformable object types? Keep broad; user asked "objects".
    if obj.location.length_squared > 1e-12:
        return True

    # Quaternion compare: identity
    if abs(obj.rotation_quaternion.w - 1.0) > 1e-12:
        return True
    if obj.rotation_quaternion.x * obj.rotation_quaternion.x + obj.rotation_quaternion.y * obj.rotation_quaternion.y + obj.rotation_quaternion.z * obj.rotation_quaternion.z > 1e-12:
        return True

    if abs(obj.scale.x - 1.0) > 1e-12 or abs(obj.scale.y - 1.0) > 1e-12 or abs(obj.scale.z - 1.0) > 1e-12:
        return True

    return False


def _armature_hierarchy_issues(arm_obj: bpy.types.Object):
    issues = []
    if arm_obj is None or arm_obj.type != 'ARMATURE':
        return issues

    # Armature object naming convention
    if arm_obj.name != "Root":
        issues.append(f"ARMATURE: armature object must be named 'Root' (found '{arm_obj.name}')")

    arm_data = arm_obj.data
    if arm_data is None:
        return issues

    bones = arm_data.bones
    if bones is None:
        return issues

    # Validate top-level bones: exactly one root-level bone named "Global".
    top_level = [b for b in bones if b.parent is None]
    if len(top_level) != 1:
        issues.append(
            f"ARMATURE: '{arm_obj.name}' must have exactly 1 top-level bone (found {len(top_level)})"
        )
        return issues

    global_bone = top_level[0]
    if global_bone.name != "Global":
        issues.append(
            f"ARMATURE: '{arm_obj.name}' top-level bone must be named 'Global' (found '{global_bone.name}')"
        )
        return issues

    # Validate Global has exactly one child named "Position".
    global_children = list(global_bone.children)
    if len(global_children) != 1:
        issues.append(
            f"ARMATURE: '{arm_obj.name}' bone 'Global' must have exactly 1 child (found {len(global_children)})"
        )
        return issues

    if global_children[0].name != "Position":
        issues.append(
            f"ARMATURE: '{arm_obj.name}' bone 'Global' child must be named 'Position' (found '{global_children[0].name}')"
        )

    return issues


def _has_armature_modifier(mesh_obj: bpy.types.Object) -> bool:
    if mesh_obj is None or mesh_obj.type != 'MESH':
        return True

    for m in mesh_obj.modifiers:
        if m.type == 'ARMATURE':
            return True
    return False


def _armature_has_pose(arm_obj: bpy.types.Object) -> bool:
    if arm_obj is None or arm_obj.type != 'ARMATURE':
        return False

    pose_bones = arm_obj.pose.bones
    for pb in pose_bones:
        # Compare local pose transform to identity.
        # pb.matrix_basis is the delta from rest pose.
        mb: Matrix = pb.matrix_basis
        if not mb.is_identity:
            return True
    return False


def _refresh_report_text(context, lines):
    wm = context.window_manager
    wm.uma_tools_report_lines.clear()

    if lines:
        for s in lines:
            item = wm.uma_tools_report_lines.add()
            item.text = s
    else:
        item = wm.uma_tools_report_lines.add()
        item.text = "No issues found."


class UMA_OT_tools_check_errors(bpy.types.Operator):
    bl_idname = "uma_tools.check_errors"
    bl_label = "Check for Errors"
    bl_options = {'REGISTER'}

    selected_only: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        targets = _iter_target_objects(context, self.selected_only)

        issues = []

        # Transform checks
        for obj in targets:
            if _needs_transform_apply(obj):
                issues.append(f"TRANSFORM: '{obj.name}' has unapplied transforms")

        # Armature pose checks
        for obj in targets:
            if obj.type == 'ARMATURE':
                if _armature_has_pose(obj):
                    issues.append(f"ARMATURE: '{obj.name}' has pose bones not in rest pose")

                issues.extend(_armature_hierarchy_issues(obj))

        # Mesh armature modifier checks
        for obj in targets:
            if obj.type == 'MESH':
                if not _has_armature_modifier(obj):
                    issues.append(f"MESH: '{obj.name}' is missing an Armature modifier")

        _refresh_report_text(context, issues)
        return {'FINISHED'}


class UMA_OT_tools_insert_global_position_bones(bpy.types.Operator):
    bl_idname = "uma_tools.insert_global_position_bones"
    bl_label = "Insert Global/Position bones"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm_obj = context.view_layer.objects.active
        if arm_obj is None or arm_obj.type != 'ARMATURE':
            arm_obj = next((o for o in context.selected_objects if o is not None and o.type == 'ARMATURE'), None)

        if arm_obj is None:
            self.report({'WARNING'}, "Select an armature (or make it the active object)")
            return {'CANCELLED'}

        arm = arm_obj.data
        if arm is None:
            self.report({'WARNING'}, "Selected armature has no data")
            return {'CANCELLED'}

        prev_mode = arm_obj.mode
        try:
            if prev_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')

            ebones = arm.edit_bones
            if ebones is None:
                self.report({'ERROR'}, "Unable to access edit bones")
                return {'CANCELLED'}

            # Capture current top-level bones BEFORE changes.
            top_level = [b for b in ebones if b.parent is None]

            # Reuse Global only if it's the top-level bone. If the top-level bone is named "global",
            # rename it to "Global".
            global_bone = None
            if len(top_level) == 1:
                if top_level[0].name == "Global":
                    global_bone = top_level[0]
                elif top_level[0].name == "global":
                    top_level[0].name = "Global"
                    global_bone = top_level[0]

            # Otherwise create a new Global at the top-level (do not touch any other "Global" deeper in the rig).
            if global_bone is None:
                global_bone = ebones.new("Global")
                global_bone.parent = None

            # Place Global at armature local origin with no transform (head at 0,0,0).
            global_bone.head = (0.0, 0.0, 0.0)
            global_bone.tail = (0.0, 0.0, 0.1)
            global_bone.roll = 0.0

            # Ensure Position is the first/only child of Global.
            # If Global already has a first child named "position", rename it to "Position".
            # If not, create a new "Position" (do not touch any other "Position" deeper in the rig).
            global_children = [b for b in ebones if b.parent == global_bone]
            position_bone = None
            if global_children:
                if global_children[0].name == "Position":
                    position_bone = global_children[0]
                elif global_children[0].name == "position":
                    global_children[0].name = "Position"
                    position_bone = global_children[0]

            if position_bone is None:
                position_bone = ebones.new("Position")
                position_bone.parent = global_bone

            # Place Position at origin too (aligned with Global). Keep a small length.
            position_bone.head = (0.0, 0.0, 0.0)
            position_bone.tail = (0.0, 0.0, 0.1)
            position_bone.roll = 0.0

            # Reparent any previous top-level bones (except Global) under Position.
            for b in top_level:
                if b == global_bone:
                    continue
                b.parent = position_bone

        finally:
            # Restore previous mode
            if prev_mode != arm_obj.mode:
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except Exception:
                    bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, f"Inserted/validated Global -> Position on '{arm_obj.name}'")
        return {'FINISHED'}


class UMA_OT_tools_copy_weights_to_selected(bpy.types.Operator):
    bl_idname = "uma_tools.copy_weights_to_selected"
    bl_label = "Copy Weights to All Selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        wm = context.window_manager
        source = getattr(wm, "uma_tools_weights_source", None)
        if source is None or source.type != 'MESH':
            self.report({'WARNING'}, "Pick a mesh source object for weights")
            return {'CANCELLED'}

        targets = [o for o in context.selected_objects if o is not None and o.type == 'MESH' and o != source]
        if not targets:
            self.report({'WARNING'}, "No target meshes selected")
            return {'CANCELLED'}

        prev_active = context.view_layer.objects.active
        prev_sel = list(context.selected_objects)

        try:
            for obj in targets:
                # Create Data Transfer modifier
                mod = obj.modifiers.new(name="UMA_CopyWeights", type='DATA_TRANSFER')
                mod.object = source

                # Vertex groups (weights)
                mod.use_vert_data = True
                mod.data_types_verts = {'VGROUP_WEIGHTS'}

                # Mapping as requested
                mod.vert_mapping = 'NEAREST_POLY'

                # Mix settings
                mod.mix_mode = 'REPLACE'
                mod.mix_factor = 1.0

                # Apply modifier (requires object to be active)
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                bpy.ops.object.modifier_apply(modifier=mod.name)

        except RuntimeError as e:
            self.report({'ERROR'}, f"Copy weights failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for o in prev_sel:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

        self.report({'INFO'}, f"Copied weights from '{source.name}' to {len(targets)} mesh(es)")
        return {'FINISHED'}


class UMA_OT_tools_process_rename_selected(bpy.types.Operator):
    bl_idname = "uma_tools.process_rename_selected"
    bl_label = "Process rename on selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        wm = context.window_manager
        prefix = (getattr(wm, "uma_tools_rename_prepend", "") or "")
        suffix = (getattr(wm, "uma_tools_rename_append", "") or "")

        targets = [o for o in context.selected_objects if o is not None and o.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        changed = 0
        for obj in targets:
            new_name = obj.name

            if prefix:
                if not new_name.startswith(prefix):
                    new_name = prefix + new_name

            if suffix:
                if not new_name.endswith(suffix):
                    new_name = new_name + suffix

            if new_name != obj.name:
                obj.name = new_name
                changed += 1

        self.report({'INFO'}, f"Renamed {changed} of {len(targets)} mesh(es)")
        return {'FINISHED'}


class UMA_OT_tools_remove_empty_vertex_groups(bpy.types.Operator):
    bl_idname = "uma_tools.remove_empty_vertex_groups"
    bl_label = "Remove empty vertex groups"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        targets = [o for o in context.selected_objects if o is not None and o.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        removed_total = 0

        for obj in targets:
            # Build set of groups that have any non-zero weight.
            used_groups = set()
            for v in obj.data.vertices:
                for g in v.groups:
                    if g.weight > 0.0:
                        used_groups.add(g.group)

            # Remove groups (iterate in reverse to keep indices stable).
            for idx in range(len(obj.vertex_groups) - 1, -1, -1):
                if idx not in used_groups:
                    obj.vertex_groups.remove(obj.vertex_groups[idx])
                    removed_total += 1

        self.report({'INFO'}, f"Removed {removed_total} empty vertex group(s)")
        return {'FINISHED'}


class UMA_OT_tools_fix_transforms(bpy.types.Operator):
    bl_idname = "uma_tools.fix_transforms"
    bl_label = "Fix"
    bl_options = {'REGISTER', 'UNDO'}

    selected_only: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        targets = _iter_target_objects(context, self.selected_only)

        # Apply transforms to objects that need it.
        # Do not apply armature pose to rest pose (explicitly required).
        prev_active = context.view_layer.objects.active
        prev_sel = list(context.selected_objects)

        try:
            for obj in targets:
                if obj is None:
                    continue

                if not _needs_transform_apply(obj):
                    continue

                # Blender ops require active + selected.
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj

                try:
                    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
                except RuntimeError:
                    # Ignore objects that can’t be applied due to mode/constraints
                    pass
        finally:
            # Restore selection
            bpy.ops.object.select_all(action='DESELECT')
            for o in prev_sel:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

        # Re-run checks after fix
        bpy.ops.uma_tools.check_errors(selected_only=self.selected_only)
        return {'FINISHED'}


class UMA_OT_tools_select_all(bpy.types.Operator):
    bl_idname = "uma_tools.select_all"
    bl_label = "Select All"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        bpy.ops.object.select_all(action='DESELECT')
        first_armature = None
        for obj in context.scene.objects:
            if obj is None:
                continue
            if obj.type in {'MESH', 'ARMATURE'}:
                obj.select_set(True)
                if first_armature is None and obj.type == 'ARMATURE':
                    first_armature = obj

        if first_armature is not None:
            context.view_layer.objects.active = first_armature

        return {'FINISHED'}


class UMA_OT_tools_fbx_export_all(bpy.types.Operator):
    bl_idname = "uma_tools.fbx_export_all"
    bl_label = "UMA FBX Export (All)"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", default="")
    filter_glob: bpy.props.StringProperty(default="*.fbx", options={'HIDDEN'})

    def execute(self, context):
        if not self.filepath:
            self.report({'WARNING'}, "No file path specified")
            return {'CANCELLED'}

        if not self.filepath.lower().endswith('.fbx'):
            self.filepath += '.fbx'

        try:
            bpy.ops.export_scene.fbx(
                filepath=self.filepath,
                use_selection=False,
                use_active_collection=False,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_ALL',
                bake_space_transform=False,
                object_types={'ARMATURE', 'MESH', 'EMPTY'},
                use_mesh_modifiers=True,
                use_mesh_modifiers_render=True,
                mesh_smooth_type='OFF',
                use_subsurf=False,
                use_mesh_edges=False,
                use_tspace=False,
                use_custom_props=False,
                add_leaf_bones=True,
                primary_bone_axis='Y',
                secondary_bone_axis='X',
                armature_nodetype='NULL',
                bake_anim=False,
                bake_anim_use_all_bones=True,
                bake_anim_use_nla_strips=True,
                bake_anim_use_all_actions=True,
                bake_anim_force_startend_keying=True,
                bake_anim_step=1.0,
                bake_anim_simplify_factor=1.0,
                path_mode='COPY',
                embed_textures=True,
                batch_mode='OFF',
                use_batch_own_dir=True,
                use_metadata=True,
                axis_forward='-Z',
                axis_up='Y'
            )
            self.report({'INFO'}, f"Exported FBX: {os.path.basename(self.filepath)}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            return {'CANCELLED'}

    def invoke(self, context, event):
        self.filepath = _uma_default_export_path("_all")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class UMA_OT_tools_fbx_export_selected(bpy.types.Operator):
    bl_idname = "uma_tools.fbx_export_selected"
    bl_label = "UMA FBX Export (Selected)"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", default="")
    filter_glob: bpy.props.StringProperty(default="*.fbx", options={'HIDDEN'})

    def execute(self, context):
        if not self.filepath:
            self.report({'WARNING'}, "No file path specified")
            return {'CANCELLED'}

        if not context.selected_objects:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}

        if not self.filepath.lower().endswith('.fbx'):
            self.filepath += '.fbx'

        try:
            bpy.ops.export_scene.fbx(
                filepath=self.filepath,
                use_selection=True,
                use_active_collection=False,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_ALL',
                bake_space_transform=False,
                object_types={'ARMATURE', 'MESH', 'EMPTY'},
                use_mesh_modifiers=True,
                use_mesh_modifiers_render=True,
                mesh_smooth_type='OFF',
                use_subsurf=False,
                use_mesh_edges=False,
                use_tspace=False,
                use_custom_props=False,
                add_leaf_bones=True,
                primary_bone_axis='Y',
                secondary_bone_axis='X',
                armature_nodetype='NULL',
                bake_anim=False,
                bake_anim_use_all_bones=True,
                bake_anim_use_nla_strips=True,
                bake_anim_use_all_actions=True,
                bake_anim_force_startend_keying=True,
                bake_anim_step=1.0,
                bake_anim_simplify_factor=1.0,
                path_mode='COPY',
                embed_textures=True,
                batch_mode='OFF',
                use_batch_own_dir=True,
                use_metadata=True,
                axis_forward='-Z',
                axis_up='Y'
            )
            self.report({'INFO'}, f"Exported FBX: {os.path.basename(self.filepath)}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            return {'CANCELLED'}

    def invoke(self, context, event):
        self.filepath = _uma_default_export_path("_selected")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class UMA_PT_tools_panel(bpy.types.Panel):
    bl_label = "UMA Tools"
    bl_idname = "UMA_PT_tools_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'UMA Tools'

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        row = layout.row()
        row.label(text=f"Version: {ADDON_VERSION_STR}")

        layout.separator()

        def _fold_text(name, expanded):
            if expanded:
                return "\u25BC " + name
            return "\u25B6 " + name

        def _draw_fold_header(parent, prop_name, title):
            row = parent.row(align=True)
            row.alignment = 'LEFT'
            expanded = getattr(wm, prop_name)
            row.prop(wm, prop_name, text=_fold_text(title, expanded), emboss=False)

        # Error checking
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_error_checking", "Error checking")
        if wm.uma_tools_section_error_checking:
            col = box.column(align=True)
            op = col.operator(UMA_OT_tools_check_errors.bl_idname, text="Check for Errors")
            op.selected_only = False

            row = col.row(align=True)
            row.operator(UMA_OT_tools_select_all.bl_idname, text="Select All")
            op = row.operator(UMA_OT_tools_fix_transforms.bl_idname, text="Apply Transforms")
            op.selected_only = True

            col.operator(UMA_OT_tools_insert_global_position_bones.bl_idname, text="Insert Global/Position bones")

            col.separator()
            col.label(text="Report:")
            row = col.row()
            row.template_list(
                "UMA_UL_tools_report",
                "",
                wm,
                "uma_tools_report_lines",
                wm,
                "uma_tools_report_index",
                rows=6,
            )
            if 0 <= wm.uma_tools_report_index < len(wm.uma_tools_report_lines):
                sel = wm.uma_tools_report_lines[wm.uma_tools_report_index]
                col.separator()
                col.label(text="Selected:")
                col.label(text=getattr(sel, "text", ""))

        # Copy Weights
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_copy_weights", "Copy Weights")
        if wm.uma_tools_section_copy_weights:
            col = box.column(align=True)
            col.prop(wm, "uma_tools_weights_source", text="Source")
            col.operator(UMA_OT_tools_copy_weights_to_selected.bl_idname, text="Copy weights to all selected")

        # Utilities
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_utilities", "Utilities")
        if wm.uma_tools_section_utilities:
            util = box.column(align=True)
            util.prop(wm, "uma_tools_rename_prepend", text="Prepend")
            util.prop(wm, "uma_tools_rename_append", text="Append")
            util.operator(UMA_OT_tools_process_rename_selected.bl_idname, text="Process rename on selected")
            util.separator()
            util.operator(UMA_OT_tools_remove_empty_vertex_groups.bl_idname, text="Remove empty vertex groups")

        # UMA Export
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_export", "UMA Export")
        if wm.uma_tools_section_export:
            box.operator_context = 'INVOKE_DEFAULT'
            box.operator(UMA_OT_tools_fbx_export_all.bl_idname, text="Export FBX (All)")
            box.operator(UMA_OT_tools_fbx_export_selected.bl_idname, text="Export FBX (Selected)")
            box.operator_context = 'EXEC_DEFAULT'

class UMA_ToolsReportLine(bpy.types.PropertyGroup):
    text: bpy.props.StringProperty(default="")


class UMA_UL_tools_report(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=getattr(item, "text", ""), icon='ERROR')
        else:
            layout.label(text="")


classes = (
    UMA_ToolsReportLine,
    UMA_UL_tools_report,
    UMA_OT_tools_check_errors,
    UMA_OT_tools_insert_global_position_bones,
    UMA_OT_tools_select_all,
    UMA_OT_tools_fix_transforms,
    UMA_OT_tools_copy_weights_to_selected,
    UMA_OT_tools_process_rename_selected,
    UMA_OT_tools_remove_empty_vertex_groups,
    UMA_OT_tools_fbx_export_all,
    UMA_OT_tools_fbx_export_selected,
    UMA_PT_tools_panel,
)


def register():
    # Allow re-running from the Text Editor without duplicate registrations.
    for cls in classes:
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
        bpy.utils.register_class(cls)

    # Avoid bpy.data.* access in register (may be restricted during install).
    if hasattr(bpy.types.WindowManager, "uma_tools_report_lines"):
        try:
            del bpy.types.WindowManager.uma_tools_report_lines
        except Exception:
            pass
    if hasattr(bpy.types.WindowManager, "uma_tools_report_index"):
        try:
            del bpy.types.WindowManager.uma_tools_report_index
        except Exception:
            pass

    bpy.types.WindowManager.uma_tools_report_lines = bpy.props.CollectionProperty(type=UMA_ToolsReportLine)
    bpy.types.WindowManager.uma_tools_report_index = bpy.props.IntProperty(default=0)

    if hasattr(bpy.types.WindowManager, "uma_tools_weights_source"):
        try:
            del bpy.types.WindowManager.uma_tools_weights_source
        except Exception:
            pass
    bpy.types.WindowManager.uma_tools_weights_source = bpy.props.PointerProperty(
        name="Weights Source",
        description="Source mesh object to copy vertex group weights from",
        type=bpy.types.Object,
    )

    if hasattr(bpy.types.WindowManager, "uma_tools_rename_prepend"):
        try:
            del bpy.types.WindowManager.uma_tools_rename_prepend
        except Exception:
            pass
    if hasattr(bpy.types.WindowManager, "uma_tools_rename_append"):
        try:
            del bpy.types.WindowManager.uma_tools_rename_append
        except Exception:
            pass

    bpy.types.WindowManager.uma_tools_rename_prepend = bpy.props.StringProperty(
        name="Prepend",
        description="String to prepend to selected mesh object names",
        default="",
    )
    bpy.types.WindowManager.uma_tools_rename_append = bpy.props.StringProperty(
        name="Append",
        description="String to append to selected mesh object names",
        default="",
    )

    # UI foldouts
    for prop_name, default in (
        ("uma_tools_section_error_checking", True),
        ("uma_tools_section_copy_weights", False),
        ("uma_tools_section_utilities", False),
        ("uma_tools_section_export", True),
    ):
        if hasattr(bpy.types.WindowManager, prop_name):
            try:
                delattr(bpy.types.WindowManager, prop_name)
            except Exception:
                pass
        setattr(
            bpy.types.WindowManager,
            prop_name,
            bpy.props.BoolProperty(name=prop_name, default=default),
        )


def unregister():
    if hasattr(bpy.types.WindowManager, "uma_tools_report_lines"):
        try:
            del bpy.types.WindowManager.uma_tools_report_lines
        except Exception:
            pass
    if hasattr(bpy.types.WindowManager, "uma_tools_report_index"):
        try:
            del bpy.types.WindowManager.uma_tools_report_index
        except Exception:
            pass

    if hasattr(bpy.types.WindowManager, "uma_tools_weights_source"):
        try:
            del bpy.types.WindowManager.uma_tools_weights_source
        except Exception:
            pass

    if hasattr(bpy.types.WindowManager, "uma_tools_rename_prepend"):
        try:
            del bpy.types.WindowManager.uma_tools_rename_prepend
        except Exception:
            pass
    if hasattr(bpy.types.WindowManager, "uma_tools_rename_append"):
        try:
            del bpy.types.WindowManager.uma_tools_rename_append
        except Exception:
            pass

    for prop_name in (
        "uma_tools_section_error_checking",
        "uma_tools_section_copy_weights",
        "uma_tools_section_utilities",
        "uma_tools_section_export",
    ):
        if hasattr(bpy.types.WindowManager, prop_name):
            try:
                delattr(bpy.types.WindowManager, prop_name)
            except Exception:
                pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
