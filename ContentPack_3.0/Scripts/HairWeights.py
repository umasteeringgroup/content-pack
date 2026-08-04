# ------------------------------
# UMATools Ponytail Weights Add-on Section
# Usage:
# - In the UMA Tools sidebar, open "Ponytail Weights".
# - Add selected bones from the active armature, set Smooth Factor (0.0-1.0).
# - Select mesh vertices (or none to process all), then press "Calculate weights for bones".
# Notes:
# - This creates vertex groups named exactly like the bones and limits weighting to those groups.
# - Smoothing uses a Gaussian mapping from smooth_factor -> sigma (0 => nearest-only, 1 => broad blend).
# ------------------------------

import time
import math
from mathutils import Vector

# ------------------------------------------------------------------
# Property group and registration helpers
# ------------------------------------------------------------------
class UMAToolsPonyBoneItem(bpy.types.PropertyGroup):
    bone_name: bpy.props.StringProperty(name="Bone Name")


class UMATools_OT_add_selected_pony_bones(bpy.types.Operator):
    """Add selected bones from the active armature to the ponytail bone list (no duplicates)."""
    bl_idname = "umatools.pony_add_selected_bones"
    bl_label = "Add Selected Bones"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        items = scene.umatools_pony_bones
        names = {it.bone_name for it in items}

        arm_obj = context.view_layer.objects.active
        if arm_obj is None or arm_obj.type != 'ARMATURE':
            arm_obj = next((o for o in context.selected_objects if o is not None and o.type == 'ARMATURE'), None)
        if arm_obj is None:
            self.report({'WARNING'}, "No active or selected armature found.")
            return {'CANCELLED'}

        sel_pose_bones = [pb for pb in arm_obj.data.bones if getattr(pb, "select", False)]
        if not sel_pose_bones:
            # Try pose bones selection (pose mode)
            sel_pose_bones = [pb for pb in arm_obj.pose.bones if getattr(pb, "bone", None) and getattr(pb.bone, "select", False)]

        added = 0
        for b in sel_pose_bones:
            name = b.name
            if name in names:
                continue
            item = items.add()
            item.bone_name = name
            names.add(name)
            added += 1

        if added:
            self.report({'INFO'}, f"Added {added} bone(s).")
        else:
            self.report({'INFO'}, "No bones added (none selected or already present).")
        return {'FINISHED'}


class UMATools_OT_remove_selected_pony_bone(bpy.types.Operator):
    """Remove the selected bone entry from the ponytail list."""
    bl_idname = "umatools.pony_remove_selected_bone"
    bl_label = "Remove Selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        idx = scene.umatools_pony_bones_index
        if idx < 0 or idx >= len(scene.umatools_pony_bones):
            self.report({'WARNING'}, "No list item selected.")
            return {'CANCELLED'}
        scene.umatools_pony_bones.remove(idx)
        scene.umatools_pony_bones_index = max(0, min(len(scene.umatools_pony_bones) - 1, idx - 1))
        self.report({'INFO'}, "Removed selected bone entry.")
        return {'FINISHED'}


class UMATools_OT_clear_pony_bones(bpy.types.Operator):
    """Clear the ponytail bone list."""
    bl_idname = "umatools.pony_clear_bones"
    bl_label = "Clear List"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        scene.umatools_pony_bones.clear()
        scene.umatools_pony_bones_index = 0
        self.report({'INFO'}, "Cleared ponytail bone list.")
        return {'FINISHED'}


class UMATools_UL_pony_bone_list(bpy.types.UIList):
    """UIList for ponytail bones."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if item is None:
            return
        layout.label(text=item.bone_name, icon='BONE_DATA')


# ------------------------------------------------------------------
# Weight calculation operator
# ------------------------------------------------------------------
class UMATools_OT_calculate_pony_weights(bpy.types.Operator):
    """Calculate vertex weights for the selected mesh vertices limited to the chosen ponytail bones."""
    bl_idname = "umatools.calculate_pony_weights"
    bl_label = "Calculate weights for bones"
    bl_options = {'REGISTER', 'UNDO'}

    def _find_armature_for_mesh(self, mesh_obj):
        # Prefer armature modifier
        for m in mesh_obj.modifiers:
            if m.type == 'ARMATURE' and getattr(m, "object", None) is not None:
                return m.object
        # Otherwise, try selected armature
        arm = next((o for o in bpy.context.selected_objects if o.type == 'ARMATURE'), None)
        if arm:
            return arm
        # Fallback: first armature in scene
        return next((o for o in bpy.context.scene.objects if o.type == 'ARMATURE'), None)

    def execute(self, context):
        scene = context.scene
        wm = context.window_manager

        # Validate mesh
        mesh_obj = context.view_layer.objects.active
        if mesh_obj is None or mesh_obj.type != 'MESH':
            self.report({'ERROR'}, "Active object must be a mesh.")
            return {'CANCELLED'}

        # Determine vertices to process
        mesh_data = mesh_obj.data
        if mesh_data is None:
            self.report({'ERROR'}, "Active mesh has no data.")
            return {'CANCELLED'}

        selected_verts = [v for v in mesh_data.vertices if v.select]
        use_selected_only = len(selected_verts) > 0
        verts_to_process = selected_verts if use_selected_only else list(mesh_data.vertices)
        total_verts = len(verts_to_process)
        if total_verts == 0:
            self.report({'WARNING'}, "No vertices to process.")
            return {'CANCELLED'}

        # Find armature
        arm_obj = self._find_armature_for_mesh(mesh_obj)
        if arm_obj is None:
            self.report({'ERROR'}, "No armature found for the mesh (no Armature modifier or selected armature).")
            return {'CANCELLED'}

        # Build bone list from scene collection
        bone_names = [it.bone_name for it in scene.umatools_pony_bones if it.bone_name]
        if not bone_names:
            self.report({'ERROR'}, "Ponytail bone list is empty.")
            return {'CANCELLED'}

        # Validate bones exist in armature
        arm_data = arm_obj.data
        existing_bones = set(b.name for b in arm_data.bones)
        used_bones = [n for n in bone_names if n in existing_bones]
        missing = [n for n in bone_names if n not in existing_bones]
        if not used_bones:
            self.report({'ERROR'}, "None of the listed bones exist in the armature.")
            return {'CANCELLED'}

        # Ensure vertex groups exist for each used bone
        vg_map = {}
        for bname in used_bones:
            vg = mesh_obj.vertex_groups.get(bname)
            if vg is None:
                vg = mesh_obj.vertex_groups.new(name=bname)
            vg_map[bname] = vg

        # Remove vertices from groups not in chosen list (only for vertices we will process)
        # Build set of allowed group indices
        allowed_group_names = set(used_bones)
        allowed_indices = {g.index for g in mesh_obj.vertex_groups if g.name in allowed_group_names}

        # Precompute bone world positions (use pose bones head in world space)
        pose_bones = arm_obj.pose.bones
        bone_positions = []
        for bname in used_bones:
            pb = pose_bones.get(bname)
            if pb is None:
                bone_positions.append(None)
            else:
                bone_positions.append(arm_obj.matrix_world @ pb.head)

        # Prepare for processing
        min_sigma = 1e-6
        smooth_factor = max(0.0, min(1.0, getattr(scene, "umatools_pony_smooth", 0.5)))
        start_time = time.perf_counter()

        # Progress
        try:
            wm.progress_begin(0, total_verts)
        except Exception:
            pass

        # Switch to object mode to safely modify vertex groups
        prev_mode = mesh_obj.mode
        try:
            if prev_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

        processed = 0
        try:
            # For performance, localize frequently used names
            mw = mesh_obj.matrix_world
            vgroups = mesh_obj.vertex_groups
            all_groups = list(mesh_obj.vertex_groups)

            # Precompute bone positions per vertex loop (we'll compute per vertex)
            for i, v in enumerate(verts_to_process):
                # Update progress occasionally
                if (i & 0x3FF) == 0:  # every 1024 vertices
                    try:
                        wm.progress_update(i)
                    except Exception:
                        pass

                # Remove vertex from any group not in allowed list
                # We only remove for groups that actually contain this vertex (check vertex.groups)
                for g in v.groups:
                    gidx = g.group
                    if gidx not in allowed_indices:
                        # Remove this vertex from that group
                        try:
                            mesh_obj.vertex_groups[gidx].remove([v.index])
                        except Exception:
                            # ignore removal errors
                            pass

                # Compute world-space vertex position
                v_world = mw @ v.co

                # Compute distances to each bone position
                distances = []
                max_d = 0.0
                for bp in bone_positions:
                    if bp is None:
                        d = float('inf')
                    else:
                        d = (v_world - bp).length
                    distances.append(d)
                    if d != float('inf') and d > max_d:
                        max_d = d

                # Determine sigma from smooth_factor
                if max_d <= 0.0:
                    sigma = min_sigma
                else:
                    max_sigma = max_d * 0.75
                    sigma = (min_sigma * (1.0 - smooth_factor)) + (max_sigma * smooth_factor)

                # If smooth_factor == 0 (sigma extremely small), do nearest-only
                weights = [0.0] * len(used_bones)
                if smooth_factor <= 1e-6 or sigma <= (min_sigma * 10.0):
                    # nearest-only
                    best_idx = None
                    best_d = float('inf')
                    for bi, d in enumerate(distances):
                        if d < best_d:
                            best_d = d
                            best_idx = bi
                    if best_idx is not None and best_d != float('inf'):
                        weights[best_idx] = 1.0
                else:
                    # Gaussian radial basis: weight_i = exp(-d_i^2 / (2*sigma^2))
                    # Gaussian is stable, smooth, and parameterizable; preferred over naive inverse-distance.
                    two_sigma_sq = 2.0 * sigma * sigma
                    total = 0.0
                    for bi, d in enumerate(distances):
                        if d == float('inf'):
                            w = 0.0
                        else:
                            w = math.exp(-(d * d) / two_sigma_sq)
                        weights[bi] = w
                        total += w
                    if total > 0.0:
                        inv_total = 1.0 / total
                        for bi in range(len(weights)):
                            weights[bi] *= inv_total
                    else:
                        # fallback to nearest-only if numerical underflow
                        best_idx = None
                        best_d = float('inf')
                        for bi, d in enumerate(distances):
                            if d < best_d:
                                best_d = d
                                best_idx = bi
                        if best_idx is not None and best_d != float('inf'):
                            weights = [0.0] * len(used_bones)
                            weights[best_idx] = 1.0

                # Assign weights to vertex groups (REPLACE)
                for bi, bname in enumerate(used_bones):
                    w = weights[bi]
                    if w > 1e-6:
                        try:
                            vgroups[bname].add([v.index], w, 'REPLACE')
                        except Exception:
                            # fallback by index
                            try:
                                vgroups[vg_map[bname].index].add([v.index], w, 'REPLACE')
                            except Exception:
                                pass
                    else:
                        # Ensure small weights are removed
                        try:
                            vgroups[bname].remove([v.index])
                        except Exception:
                            pass

                processed += 1

        except Exception as ex:
            self.report({'ERROR'}, f"Error during weighting: {ex}")
            try:
                wm.progress_end()
            except Exception:
                pass
            return {'CANCELLED'}
        finally:
            try:
                wm.progress_end()
            except Exception:
                pass
            # restore mode
            try:
                if prev_mode != mesh_obj.mode:
                    bpy.ops.object.mode_set(mode=prev_mode)
            except Exception:
                pass

        elapsed = time.perf_counter() - start_time
        summary = f"Processed {processed} vertices; used {len(used_bones)} bones; time {elapsed:.2f}s"
        if missing:
            summary += f"; skipped {len(missing)} missing bones"
        self.report({'INFO'}, summary)
        print(summary)
        return {'FINISHED'}


# ------------------------------------------------------------------
# Panel UI
# ------------------------------------------------------------------
class UMATools_PT_pony_tail_weights(bpy.types.Panel):
    bl_label = "Ponytail Weights"
    bl_idname = "UMATools_PT_pony_tail_weights"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UMA Tools"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        row = layout.row()
        row.template_list("UMATools_UL_pony_bone_list", "", scene, "umatools_pony_bones", scene, "umatools_pony_bones_index", rows=4)

        col = row.column(align=True)
        col.operator("umatools.pony_add_selected_bones", icon='ADD', text="")
        col.operator("umatools.pony_remove_selected_bone", icon='REMOVE', text="")
        col.operator("umatools.pony_clear_bones", icon='TRASH', text="")

        layout.prop(scene, "umatools_pony_smooth", slider=True)
        layout.operator("umatools.calculate_pony_weights", icon='MOD_VERTEX_WEIGHT')


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------
classes = (
    UMAToolsPonyBoneItem,
    UMATools_UL_pony_bone_list,
    UMATools_OT_add_selected_pony_bones,
    UMATools_OT_remove_selected_pony_bone,
    UMATools_OT_clear_pony_bones,
    UMATools_OT_calculate_pony_weights,
    UMATools_PT_pony_tail_weights,
)


def register_umatools_pony():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass

    bpy.types.Scene.umatools_pony_bones = bpy.props.CollectionProperty(type=UMAToolsPonyBoneItem)
    bpy.types.Scene.umatools_pony_bones_index = bpy.props.IntProperty(name="Pony Bone Index", default=0, min=0)
    bpy.types.Scene.umatools_pony_smooth = bpy.props.FloatProperty(
        name="Smooth Factor",
        description="0 = nearest-only, 1 = broad smooth blending",
        default=0.5,
        min=0.0,
        max=1.0,
    )


def unregister_umatools_pony():
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    for prop in ("umatools_pony_bones", "umatools_pony_bones_index", "umatools_pony_smooth"):
        try:
            delattr(bpy.types.Scene, prop)
        except Exception:
            pass


# If the main UMATools script already registers classes, call register_umatools_pony() from there.
# Otherwise, allow standalone registration for testing:
if __name__ == "__main__":
    register_umatools_pony()
