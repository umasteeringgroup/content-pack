bl_info = {
    "name": "Outliner: Parent Menu Shortcut",
    "author": "AI",
    "version": (1, 0, 6),
    "blender": (2, 93, 0),
    "location": "Outliner > Right Click > Object",
    "description": "Adds Parent operations to Outliner right-click Object context menu",
    "category": "Outliner",
}

import bpy
import os


def _uma_default_export_path(suffix):
    if bpy.data.filepath:
        blend_dir = os.path.dirname(bpy.data.filepath)
        blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
        return os.path.join(blend_dir, blend_name + suffix + ".fbx")

    tmp_dir = bpy.app.tempdir
    if tmp_dir:
        return os.path.join(tmp_dir, "uma" + suffix + ".fbx")

    return os.path.join(os.path.expanduser("~"), "uma" + suffix + ".fbx")


class UMA_OT_outliner_parent_set_object(bpy.types.Operator):
    """Set Parent (same as 3D View: Object > Parent > Object)"""
    bl_idname = "uma_outliner.parent_set_object"
    bl_label = "Set Parent (Object)"
    bl_options = {'REGISTER', 'UNDO'}

    keep_transform: bpy.props.BoolProperty(
        name="Keep Transform",
        description="Keep transform when parenting (Keep Transform)",
        default=True,
    )

    def execute(self, context):
        bpy.ops.object.parent_set(type='OBJECT', keep_transform=self.keep_transform)
        return {'FINISHED'}


class UMA_OT_outliner_parent_clear(bpy.types.Operator):
    """Clear Parent (same as 3D View: Object > Parent > Clear Parent)"""
    bl_idname = "uma_outliner.parent_clear"
    bl_label = "Clear Parent"
    bl_options = {'REGISTER', 'UNDO'}

    clear_type: bpy.props.EnumProperty(
        name="Clear Type",
        items=[
            ('CLEAR', "Clear Parent", "Clear parent, keep transformation"),
            ('CLEAR_KEEP_TRANSFORM', "Clear and Keep Transform", "Clear parent and keep transform"),
            ('CLEAR_INVERSE', "Clear Parent Inverse", "Clear inverse parent matrix"),
        ],
        default='CLEAR_KEEP_TRANSFORM',
    )

    def execute(self, context):
        bpy.ops.object.parent_clear(type=self.clear_type)
        return {'FINISHED'}


class UMA_OT_outliner_apply_all_transforms(bpy.types.Operator):
    """Apply All Transforms (Location, Rotation, Scale) to selected objects"""
    bl_idname = "uma_outliner.apply_all_transforms"
    bl_label = "Apply All Transforms"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objects = context.selected_objects
        
        if not selected_objects:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}
        
        # Apply transforms to all selected objects
        for obj in selected_objects:
            if obj.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'ARMATURE', 'LATTICE', 'EMPTY', 'GPENCIL', 'CAMERA', 'LIGHT', 'SPEAKER'}:
                # Ensure object is the active object for the operation
                context.view_layer.objects.active = obj
                
                try:
                    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
                except RuntimeError as e:
                    self.report({'WARNING'}, f"Could not apply transforms to {obj.name}: {str(e)}")
        
        self.report({'INFO'}, f"Applied transforms to {len(selected_objects)} object(s)")
        return {'FINISHED'}


class UMA_OT_outliner_generate_and_apply_data_transfer(bpy.types.Operator):
    """Generate data layers and apply Data Transfer modifiers on selected objects"""
    bl_idname = "uma_outliner.generate_apply_data_transfer"
    bl_label = "Generate Layers and Apply Data Transfer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objects = context.selected_objects
        
        if not selected_objects:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}
        
        objects_processed = 0
        modifiers_applied = 0
        
        for obj in selected_objects:
            # Only process mesh objects
            if obj.type != 'MESH':
                continue
            
            # Set as active object
            context.view_layer.objects.active = obj
            
            # Find Data Transfer modifiers
            data_transfer_modifiers = [mod for mod in obj.modifiers if mod.type == 'DATA_TRANSFER']
            
            if not data_transfer_modifiers:
                continue
            
            objects_processed += 1
            
            for mod in data_transfer_modifiers:
                try:
                    # Select the object
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    context.view_layer.objects.active = obj
                    
                    # Generate data layers for this modifier
                    # This creates the necessary data layers (e.g., UVs, vertex colors) on the target mesh
                    bpy.ops.object.datalayout_transfer(modifier=mod.name)
                    
                    # Apply the modifier
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                    
                    modifiers_applied += 1
                    
                except RuntimeError as e:
                    self.report({'WARNING'}, f"Could not process Data Transfer modifier '{mod.name}' on {obj.name}: {str(e)}")
                except Exception as e:
                    self.report({'WARNING'}, f"Error processing {obj.name}: {str(e)}")
        
        if modifiers_applied > 0:
            self.report({'INFO'}, f"Processed {objects_processed} object(s), applied {modifiers_applied} Data Transfer modifier(s)")
        else:
            self.report({'WARNING'}, "No Data Transfer modifiers found on selected objects")
        
        return {'FINISHED'}


class UMA_OT_outliner_fbx_export_all(bpy.types.Operator):
    """Export all objects to FBX with UMA-compatible settings"""
    bl_idname = "uma_outliner.fbx_export_all"
    bl_label = "UMA FBX Export (All)"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(
        subtype="FILE_PATH",
        default=""
    )
    
    filter_glob: bpy.props.StringProperty(
        default="*.fbx",
        options={'HIDDEN'}
    )
    
    def execute(self, context):
        if not self.filepath:
            self.report({'WARNING'}, "No file path specified")
            return {'CANCELLED'}
        
        # Ensure .fbx extension
        if not self.filepath.lower().endswith('.fbx'):
            self.filepath += '.fbx'
        
        try:
            # Export with UMA-compatible settings
            bpy.ops.export_scene.fbx(
                filepath=self.filepath,
                use_selection=False,  # Export all objects
                use_active_collection=False,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_ALL',  # Apply Scalings = FBX All
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
                bake_anim=False,  # Disable animation export
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
            
            self.report({'INFO'}, f"Exported all objects to {os.path.basename(self.filepath)}")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            return {'CANCELLED'}
    
    def invoke(self, context, event):
        self.filepath = _uma_default_export_path("_all")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class UMA_OT_outliner_fbx_export_selected(bpy.types.Operator):
    """Export selected objects to FBX with UMA-compatible settings"""
    bl_idname = "uma_outliner.fbx_export_selected"
    bl_label = "UMA FBX Export (Selected)"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(
        subtype="FILE_PATH",
        default=""
    )
    
    filter_glob: bpy.props.StringProperty(
        default="*.fbx",
        options={'HIDDEN'}
    )
    
    def execute(self, context):
        if not self.filepath:
            self.report({'WARNING'}, "No file path specified")
            return {'CANCELLED'}
        
        if not context.selected_objects:
            self.report({'WARNING'}, "No objects selected")
            return {'CANCELLED'}
        
        # Ensure .fbx extension
        if not self.filepath.lower().endswith('.fbx'):
            self.filepath += '.fbx'
        
        try:
            # Export with UMA-compatible settings
            bpy.ops.export_scene.fbx(
                filepath=self.filepath,
                use_selection=True,  # Limit to selected objects
                use_active_collection=False,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_ALL',  # Apply Scalings = FBX All
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
                bake_anim=False,  # Disable animation export
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
            
            self.report({'INFO'}, f"Exported {len(context.selected_objects)} selected object(s) to {os.path.basename(self.filepath)}")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            return {'CANCELLED'}
    
    def invoke(self, context, event):
        self.filepath = _uma_default_export_path("_selected")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

def draw_outliner_object_context(self, context):
    layout = self.layout
    layout.separator()
    layout.label(text="Parent", icon='CONSTRAINT_BONE')

    op = layout.operator(UMA_OT_outliner_parent_set_object.bl_idname, text="Set Parent (Object)")
    op.keep_transform = True

    op = layout.operator(UMA_OT_outliner_parent_clear.bl_idname, text="Clear Parent (Keep Transform)")
    op.clear_type = 'CLEAR_KEEP_TRANSFORM'

    op = layout.operator(UMA_OT_outliner_parent_clear.bl_idname, text="Clear Parent Inverse")
    op.clear_type = 'CLEAR_INVERSE'

    layout.separator()
    layout.operator(UMA_OT_outliner_apply_all_transforms.bl_idname, text="Apply All Transforms", icon='CHECKMARK')
    layout.operator(UMA_OT_outliner_generate_and_apply_data_transfer.bl_idname, text="Generate Layers and Apply Data Transfer", icon='MOD_DATA_TRANSFER')
    
    layout.separator()
    layout.label(text="UMA Export", icon='EXPORT')
    layout.operator_context = 'INVOKE_DEFAULT'
    layout.operator(UMA_OT_outliner_fbx_export_all.bl_idname, text="UMA FBX Export (All)", icon='SCENE_DATA')
    layout.operator(UMA_OT_outliner_fbx_export_selected.bl_idname, text="UMA FBX Export (Selected)", icon='RESTRICT_SELECT_OFF')
    layout.operator_context = 'EXEC_DEFAULT'


classes = (
    UMA_OT_outliner_parent_set_object,
    UMA_OT_outliner_parent_clear,
    UMA_OT_outliner_apply_all_transforms,
    UMA_OT_outliner_generate_and_apply_data_transfer,
    UMA_OT_outliner_fbx_export_all,
    UMA_OT_outliner_fbx_export_selected,
)


def register():
    # When running repeatedly in Blender's Text Editor, previous runs may
    # still have menu callbacks attached; remove before re-adding.
    try:
        bpy.types.OUTLINER_MT_object.remove(draw_outliner_object_context)
    except Exception:
        pass

    for cls in classes:
        # If a previous run registered the class, unregister first to avoid duplicates.
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
        try:
            bpy.utils.register_class(cls)
        except Exception:
            # If already registered, unregister then re-register
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
            bpy.utils.register_class(cls)

    bpy.types.OUTLINER_MT_object.append(draw_outliner_object_context)


def unregister():
    try:
        bpy.types.OUTLINER_MT_object.remove(draw_outliner_object_context)
    except Exception:
        pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()