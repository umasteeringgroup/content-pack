#!/usr/bin/env python3
# -*- coding: utf-8 -*-

bl_info = {
    "name": "UMA Tools",
    "author": "UMA Open Source",
    "version": (1, 0, 29),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > UMA Tools",
    "description": "Quick checks and fixes for UMA export readiness.",
    "category": "3D View",
}

import bpy
import os
import bmesh
import math
from mathutils import Matrix, kdtree


ADDON_VERSION_STR = "1.29"


def _ensure_object_mode(context, obj: bpy.types.Object):
    if obj is None:
        return
    if obj.mode == 'OBJECT':
        return
    prev_active = context.view_layer.objects.active
    try:
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass
    finally:
        context.view_layer.objects.active = prev_active


def _udim_tile_from_uv(uv) -> tuple[int, int]:
    # UDIM tiles are defined by integer offsets of UV space.
    # tile (0,0) corresponds to UDIM 1001.
    return (int(math.floor(uv[0])), int(math.floor(uv[1])))


def _udim_number_from_tile(tile_u: int, tile_v: int) -> int:
    # Standard UDIM numbering: 1001 + U + 10*V
    return 1001 + int(tile_u) + int(tile_v) * 10


def _unique_material_name(base_name: str) -> str:
    if bpy.data.materials.get(base_name) is None:
        return base_name
    i = 1
    while True:
        candidate = f"{base_name}.{i:03d}"
        if bpy.data.materials.get(candidate) is None:
            return candidate
        i += 1


def _is_udim_image(img: bpy.types.Image | None) -> bool:
    if img is None:
        return False
    try:
        if getattr(img, "source", None) == 'TILED':
            return True
    except Exception:
        pass

    try:
        fp = (getattr(img, "filepath", "") or "")
        if "<UDIM>" in fp:
            return True
    except Exception:
        pass

    return False


def _force_image_single_file(img: bpy.types.Image | None, abs_path: str):
    if img is None:
        return
    try:
        img.source = 'FILE'
    except Exception:
        pass
    for attr_name in ("filepath", "filepath_raw"):
        try:
            setattr(img, attr_name, abs_path)
        except Exception:
            pass
    try:
        # If this image has UDIM tiles, try to clear them.
        tiles = getattr(img, "tiles", None)
        if tiles is not None:
            try:
                tiles.clear()
            except Exception:
                pass
    except Exception:
        pass
    try:
        img.reload()
    except Exception:
        pass


def _try_open_image_no_udim(abs_path: str) -> bpy.types.Image | None:
    # Use operator-based open with UDIM detection disabled (if an IMAGE_EDITOR area exists).
    # This is best-effort; if it fails, callers should fall back to bpy.data.images.load.
    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return None

    img_area = None
    img_region = None
    swapped_area = None
    swapped_area_prev_type = None
    try:
        for area in screen.areas:
            if area is None:
                continue
            if area.type != 'IMAGE_EDITOR':
                continue
            img_area = area
            for r in area.regions:
                if r.type == 'WINDOW':
                    img_region = r
                    break
            break
    except Exception:
        img_area = None
        img_region = None

    # If no image editor exists, temporarily switch the first area.
    if img_area is None or img_region is None:
        try:
            swapped_area = next((a for a in screen.areas if a is not None), None)
            if swapped_area is not None:
                swapped_area_prev_type = swapped_area.type
                swapped_area.type = 'IMAGE_EDITOR'
                img_area = swapped_area
                img_region = next((r for r in swapped_area.regions if r.type == 'WINDOW'), None)
        except Exception:
            img_area = None
            img_region = None

    if img_area is None or img_region is None:
        return None

    before = set()
    try:
        for im in bpy.data.images:
            try:
                before.add(im.as_pointer())
            except Exception:
                pass
    except Exception:
        before = set()

    try:
        with bpy.context.temp_override(area=img_area, region=img_region):
            try:
                bpy.ops.image.open(
                    filepath=abs_path,
                    relative_path=False,
                    check_existing=False,
                    use_sequence_detection=False,
                    use_udim_detecting=False,
                )
            except TypeError:
                # Some builds use a slightly different arg name.
                bpy.ops.image.open(
                    filepath=abs_path,
                    relative_path=False,
                    check_existing=False,
                    use_sequence_detection=False,
                    use_udim_detection=False,
                )
    except Exception:
        return None
    finally:
        if swapped_area is not None and swapped_area_prev_type is not None:
            try:
                swapped_area.type = swapped_area_prev_type
            except Exception:
                pass

    # Pick the newest image that matches the path.
    candidates = []
    for im in bpy.data.images:
        try:
            if im.as_pointer() in before:
                continue
        except Exception:
            continue

        try:
            fp = bpy.path.abspath(getattr(im, "filepath", "") or "")
            if fp and os.path.normcase(fp) == os.path.normcase(abs_path):
                candidates.append(im)
        except Exception:
            pass

    if candidates:
        # Prefer non-UDIM image.
        for im in candidates:
            if not _is_udim_image(im):
                return im
        return candidates[-1]

    return None


def _load_single_tile_image(abs_path: str, cache: dict[str, bpy.types.Image]) -> bpy.types.Image | None:
    key = os.path.normcase(abs_path)
    if key in cache:
        return cache[key]

    img = None

    # Prefer operator open with UDIM detection disabled.
    try:
        img = _try_open_image_no_udim(abs_path)
    except Exception:
        img = None

    if img is not None:
        _force_image_single_file(img, abs_path)
        cache[key] = img
        return img

    try:
        img = bpy.data.images.load(abs_path, check_existing=False)
    except Exception:
        img = None

    if img is not None:
        _force_image_single_file(img, abs_path)

    if img is not None:
        cache[key] = img
    return img


def _insert_mapping_offset_for_image_node(nodes, links, image_node: bpy.types.Node, tile_u: int, tile_v: int, uv_map_name: str | None):
    if image_node is None:
        return

    vec_input = image_node.inputs.get('Vector')
    if vec_input is None:
        return

    mapping_node = nodes.new('ShaderNodeMapping')
    mapping_node.name = "UMA_UDIM_Mapping"
    mapping_node.label = "UMA UDIM Offset"
    try:
        mapping_node.vector_type = 'POINT'
    except Exception:
        pass
    try:
        mapping_node.inputs['Location'].default_value[0] = -float(tile_u)
        mapping_node.inputs['Location'].default_value[1] = -float(tile_v)
        mapping_node.inputs['Location'].default_value[2] = 0.0
    except Exception:
        pass

    # Wire: existing_vector -> mapping -> image
    if vec_input.is_linked and vec_input.links:
        existing_link = vec_input.links[0]
        from_socket = existing_link.from_socket
        links.remove(existing_link)
        links.new(from_socket, mapping_node.inputs['Vector'])
    else:
        if uv_map_name:
            uv_node = nodes.new('ShaderNodeUVMap')
            uv_node.uv_map = uv_map_name
            uv_out = uv_node.outputs.get('UV')
            if uv_out is not None:
                links.new(uv_out, mapping_node.inputs['Vector'])
        else:
            texcoord = nodes.new('ShaderNodeTexCoord')
            uv_out = texcoord.outputs.get('UV')
            if uv_out is not None:
                links.new(uv_out, mapping_node.inputs['Vector'])

    out_vec = mapping_node.outputs.get('Vector')
    if out_vec is not None:
        links.new(out_vec, vec_input)


def _convert_material_to_single_udim_tile(mat: bpy.types.Material, udim_number: int, tile_u: int, tile_v: int, uv_map_name: str | None, image_cache: dict[str, bpy.types.Image] | None = None):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return (0, 0)

    converted = 0
    missing = 0
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    for node in nodes:
        if getattr(node, "type", None) != 'TEX_IMAGE':
            continue

        img = getattr(node, "image", None)
        if img is None:
            continue
        if getattr(img, "source", None) != 'TILED':
            continue

        tile = None
        try:
            tile = next((t for t in img.tiles if t.number == udim_number), None)
        except Exception:
            tile = None

        tile_path = None
        if tile is not None:
            tile_path = getattr(tile, "filepath", None) or getattr(tile, "path", None)

        if not tile_path:
            missing += 1
            continue

        abs_path = bpy.path.abspath(tile_path)
        if not abs_path or not os.path.exists(abs_path):
            missing += 1
            continue

        try:
            cache = image_cache if image_cache is not None else {}
            new_img = _load_single_tile_image(abs_path, cache)
            if new_img is None:
                missing += 1
                continue
            node.image = new_img
            _insert_mapping_offset_for_image_node(nodes, links, node, tile_u, tile_v, uv_map_name)
            converted += 1
        except Exception:
            missing += 1

    return (converted, missing)


def _clone_material_for_udim_tile(original: bpy.types.Material, udim_number: int, tile_u: int, tile_v: int, uv_map_name: str | None, image_cache: dict[str, bpy.types.Image]):
    if original is None:
        return None

    clone = original.copy()
    clone.name = _unique_material_name(f"{original.name}_UDIM{udim_number}")
    clone["uma_udim_split"] = True
    clone["uma_udim_original"] = original.name
    clone["uma_udim_tile"] = int(udim_number)
    clone["uma_udim_tile_u"] = int(tile_u)
    clone["uma_udim_tile_v"] = int(tile_v)

    converted, missing = _convert_material_to_single_udim_tile(clone, udim_number, tile_u, tile_v, uv_map_name, image_cache=image_cache)
    return (clone, converted, missing)


def _get_object_active_uv_name(mesh_obj: bpy.types.Object) -> str | None:
    if mesh_obj is None or mesh_obj.type != 'MESH' or mesh_obj.data is None:
        return None
    uv_layers = getattr(mesh_obj.data, "uv_layers", None)
    if not uv_layers:
        return None
    active = getattr(uv_layers, "active", None)
    if active is not None and getattr(active, "name", None):
        return active.name
    if len(uv_layers) > 0:
        return uv_layers[0].name
    return None


def _uma_default_export_path(suffix):
    if bpy.data.filepath:
        blend_dir = os.path.dirname(bpy.data.filepath)
        blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
        return os.path.join(blend_dir, blend_name + suffix + ".fbx")

    tmp_dir = bpy.app.tempdir
    if tmp_dir:
        return os.path.join(tmp_dir, "uma" + suffix + ".fbx")

    return os.path.join(os.path.expanduser("~"), "uma" + suffix + ".fbx")


def _uma_fbx_export_kwargs(context, use_selection: bool):
    use_uma_2_format = getattr(context.window_manager, "uma_tools_export_uma2_format", False)
    axis_forward = 'Z' if use_uma_2_format else '-Z'
    axis_up = 'Y'
    primary_bone_axis = 'X' if use_uma_2_format else 'Y'
    secondary_bone_axis = '-Y' if use_uma_2_format else 'X'

    return {
        "use_selection": use_selection,
        "use_active_collection": False,
        "global_scale": 1.0,
        "apply_unit_scale": True,
        "apply_scale_options": 'FBX_SCALE_ALL',
        "bake_space_transform": False,
        "object_types": {'ARMATURE', 'MESH', 'EMPTY'},
        "use_mesh_modifiers": True,
        "use_mesh_modifiers_render": True,
        "mesh_smooth_type": 'OFF',
        "use_subsurf": False,
        "use_mesh_edges": False,
        "use_tspace": False,
        "use_custom_props": False,
        "add_leaf_bones": True,
        "primary_bone_axis": primary_bone_axis,
        "secondary_bone_axis": secondary_bone_axis,
        "armature_nodetype": 'NULL',
        "bake_anim": False,
        "bake_anim_use_all_bones": True,
        "bake_anim_use_nla_strips": True,
        "bake_anim_use_all_actions": True,
        "bake_anim_force_startend_keying": True,
        "bake_anim_step": 1.0,
        "bake_anim_simplify_factor": 1.0,
        "path_mode": 'COPY',
        "embed_textures": True,
        "batch_mode": 'OFF',
        "use_batch_own_dir": True,
        "use_metadata": True,
        "axis_forward": axis_forward,
        "axis_up": axis_up,
    }


def _iter_target_objects(context, selected_only: bool):
    if selected_only:
        objs = list(context.selected_objects)
    else:
        objs = list(context.scene.objects)

    # Only process meshes and armatures (bones) for UMA workflows.
    return [o for o in objs if o is not None and o.type in {'MESH', 'ARMATURE'}]


def _deselect_all_objects(context):
    for obj in context.view_layer.objects:
        if obj is not None and obj.select_get():
            obj.select_set(False)


def _get_selected_objects(context):
    return [obj for obj in context.view_layer.objects if obj is not None and obj.select_get()]


def _swap_left_right_name(name: str) -> str:
    if name.startswith("Left"):
        return "Right" + name[4:]
    if name.startswith("Right"):
        return "Left" + name[5:]
    return name


def _get_nonzero_vertex_group_names(mesh_obj: bpy.types.Object):
    if mesh_obj is None or mesh_obj.type != 'MESH':
        return []

    used_groups = set()
    for v in mesh_obj.data.vertices:
        for g in v.groups:
            if g.weight > 0.0:
                used_groups.add(g.group)

    names = []
    for idx in sorted(used_groups):
        if 0 <= idx < len(mesh_obj.vertex_groups):
            names.append(mesh_obj.vertex_groups[idx].name)
    return names


def _clear_vertex_groups(context, mesh_obj: bpy.types.Object):
    if mesh_obj is None or mesh_obj.type != 'MESH':
        return 0

    _ensure_object_mode(context, mesh_obj)

    removed = 0
    for idx in range(len(mesh_obj.vertex_groups) - 1, -1, -1):
        mesh_obj.vertex_groups.remove(mesh_obj.vertex_groups[idx])
        removed += 1

    return removed


def _remove_empty_vertex_groups(mesh_obj: bpy.types.Object):
    if mesh_obj is None or mesh_obj.type != 'MESH' or mesh_obj.data is None:
        return 0

    used_groups = set()
    for vertex in mesh_obj.data.vertices:
        for group in vertex.groups:
            if group.weight > 0.0001:
                used_groups.add(group.group)

    removed = 0
    for idx in range(len(mesh_obj.vertex_groups) - 1, -1, -1):
        if idx not in used_groups:
            mesh_obj.vertex_groups.remove(mesh_obj.vertex_groups[idx])
            removed += 1

    return removed


def _remove_negligible_vertex_weights(context, mesh_obj: bpy.types.Object, max_weight: float = 0.001):
    if mesh_obj is None or mesh_obj.type != 'MESH' or mesh_obj.data is None:
        return 0

    _ensure_object_mode(context, mesh_obj)

    removed = 0
    for vertex_group in list(mesh_obj.vertex_groups):
        remove_indices = []
        for vertex in mesh_obj.data.vertices:
            for assignment in vertex.groups:
                if assignment.group == vertex_group.index and assignment.weight <= max_weight:
                    remove_indices.append(vertex.index)
                    break

        if remove_indices:
            vertex_group.remove(remove_indices)
            removed += len(remove_indices)

    return removed


def _smooth_vertex_group_weights(context, mesh_obj: bpy.types.Object, factor: float = 0.25):
    if mesh_obj is None or mesh_obj.type != 'MESH' or mesh_obj.data is None:
        return (0, 0)

    _ensure_object_mode(context, mesh_obj)

    vertices = mesh_obj.data.vertices
    if not vertices or not mesh_obj.vertex_groups:
        return (0, 0)

    neighbors = [[] for _ in range(len(vertices))]
    for edge in mesh_obj.data.edges:
        v1, v2 = edge.vertices
        neighbors[v1].append(v2)
        neighbors[v2].append(v1)

    groups = list(mesh_obj.vertex_groups)
    if not groups:
        return (0, 0)

    original_totals = [0.0] * len(vertices)
    smoothed_weights = {group.index: [0.0] * len(vertices) for group in groups}

    for vertex in vertices:
        for assignment in vertex.groups:
            if assignment.group not in smoothed_weights:
                continue
            smoothed_weights[assignment.group][vertex.index] = assignment.weight
            original_totals[vertex.index] += assignment.weight

    for group in groups:
        weights = smoothed_weights[group.index]
        updated = [0.0] * len(vertices)

        for vertex in vertices:
            current_weight = weights[vertex.index]
            vertex_neighbors = neighbors[vertex.index]
            if vertex_neighbors:
                neighbor_avg = sum(weights[neighbor_index] for neighbor_index in vertex_neighbors) / len(vertex_neighbors)
                updated[vertex.index] = ((1.0 - factor) * current_weight) + (factor * neighbor_avg)
            else:
                updated[vertex.index] = current_weight

        smoothed_weights[group.index] = updated

    normalized_vertices = 0
    for vertex in vertices:
        original_total = original_totals[vertex.index]
        new_total = sum(smoothed_weights[group.index][vertex.index] for group in groups)

        if original_total > 0.0 and new_total > 0.0:
            scale = original_total / new_total
            normalized_vertices += 1
        else:
            scale = 0.0

        for group in groups:
            weight = smoothed_weights[group.index][vertex.index] * scale
            if weight > 0.0001:
                group.add([vertex.index], weight, 'REPLACE')
            else:
                group.remove([vertex.index])

    return (len(groups), normalized_vertices)


def _on_quick_select_index_update(self, context):
    obj = context.view_layer.objects.active
    if obj is None or obj.type != 'MESH':
        return

    idx = self.uma_tools_group_quick_select_index
    if idx < 0 or idx >= len(self.uma_tools_group_quick_select):
        return

    name = self.uma_tools_group_quick_select[idx].name
    vg = obj.vertex_groups.get(name)
    if vg is None:
        return

    obj.vertex_groups.active_index = vg.index


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


def _get_transform_object_name_from_line(line: str):
    if not line or not line.startswith("TRANSFORM:"):
        return None
    start = line.find("'")
    end = line.find("'", start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        return line[start + 1:end]
    return None


def _get_missing_armature_object_name_from_line(line: str):
    if not line or not line.startswith("MESH:"):
        return None
    if "missing an Armature modifier" not in line:
        return None
    start = line.find("'")
    end = line.find("'", start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        return line[start + 1:end]
    return None


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
        scene = context.scene
        source = getattr(wm, "uma_tools_weights_source", None)
        smooth_weights = getattr(wm, "uma_tools_smooth_weights", False)
        mapping = getattr(wm, "uma_tools_weight_mapping", 'POLYINTERP_NEAREST')
        if source is None or source.type != 'MESH':
            self.report({'WARNING'}, "Pick a mesh source object for weights")
            return {'CANCELLED'}

        targets = [o for o in context.selected_objects if o is not None and o.type == 'MESH' and o != source]
        if not targets:
            self.report({'WARNING'}, "No target meshes selected")
            return {'CANCELLED'}

        source_groups = _get_nonzero_vertex_group_names(source)

        prev_active = context.view_layer.objects.active
        prev_sel = _get_selected_objects(context)
        removed_empty_total = 0

        try:
            for obj in targets:
                _clear_vertex_groups(context, obj)

                for group_name in source_groups:
                    if obj.vertex_groups.get(group_name) is None:
                        obj.vertex_groups.new(name=group_name)

                # Create Data Transfer modifier
                mod = obj.modifiers.new(name="UMA_CopyWeights", type='DATA_TRANSFER')
                mod.object = source

                # Vertex groups (weights)
                mod.use_vert_data = True
                mod.data_types_verts = {'VGROUP_WEIGHTS'}

                # Mapping as requested
                mod.vert_mapping = mapping

                # Mix settings
                mod.mix_mode = 'REPLACE'
                mod.mix_factor = 1.0

                # Apply modifier (requires object to be active)
                _deselect_all_objects(context)
                obj.select_set(True)
                context.view_layer.objects.active = obj
                bpy.ops.object.modifier_apply(modifier=mod.name)

                if smooth_weights:
                    _smooth_vertex_group_weights(context, obj, factor=0.25)

                removed_empty_total += _remove_empty_vertex_groups(obj)

        except RuntimeError as e:
            self.report({'ERROR'}, f"Copy weights failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            _deselect_all_objects(context)
            for o in prev_sel:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

        if smooth_weights:
            self.report({'INFO'}, f"Copied and smoothed weights from '{source.name}' to {len(targets)} mesh(es), removed {removed_empty_total} empty vertex group(s)")
        else:
            self.report({'INFO'}, f"Copied weights from '{source.name}' to {len(targets)} mesh(es), removed {removed_empty_total} empty vertex group(s)")
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
            removed_total += _remove_empty_vertex_groups(obj)

        self.report({'INFO'}, f"Removed {removed_total} empty vertex group(s)")
        return {'FINISHED'}


class UMA_OT_tools_remove_negligible_weights(bpy.types.Operator):
    bl_idname = "uma_tools.remove_negligible_weights"
    bl_label = "Remove negligible weights"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        targets = [o for o in context.selected_objects if o is not None and o.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        removed_total = 0
        for obj in targets:
            removed_total += _remove_negligible_vertex_weights(context, obj, max_weight=0.001)

        self.report({'INFO'}, f"Removed {removed_total} negligible weight assignment(s)")
        return {'FINISHED'}


def _normalize_vertex_weights_for_indices(obj: bpy.types.Object, vertex_indices):
    normalized = 0
    skipped = 0

    for idx in vertex_indices:
        vert = obj.data.vertices[idx]
        weighted_groups = [g for g in vert.groups if g.weight > 0.0 and g.group < len(obj.vertex_groups)]
        total = sum(g.weight for g in weighted_groups)

        if total <= 0.0:
            skipped += 1
            continue

        for g in weighted_groups:
            vg = obj.vertex_groups[g.group]
            vg.add([idx], g.weight / total, 'REPLACE')

        normalized += 1

    return normalized, skipped


class UMA_OT_tools_normalize_selected_weights(bpy.types.Operator):
    bl_idname = "uma_tools.normalize_selected_weights"
    bl_label = "Normalize selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object")
            return {'CANCELLED'}

        initial_mode = obj.mode

        # Read selected vertices first (especially when coming from Edit Mode).
        if initial_mode == 'EDIT':
            bm = bmesh.from_edit_mesh(obj.data)
            selected_indices = [v.index for v in bm.verts if v.select]
        else:
            selected_indices = [v.index for v in obj.data.vertices if v.select]

        if not selected_indices:
            self.report({'WARNING'}, "No selected vertices found")
            return {'CANCELLED'}

        normalized = 0
        skipped = 0

        try:
            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            normalized, skipped = _normalize_vertex_weights_for_indices(obj, selected_indices)

        except RuntimeError as e:
            self.report({'ERROR'}, f"Normalize selected failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            if obj.mode != initial_mode:
                try:
                    bpy.ops.object.mode_set(mode=initial_mode)
                except Exception:
                    pass

        if skipped:
            self.report({'INFO'}, f"Normalized {normalized} vertex(es); skipped {skipped} without weights")
        else:
            self.report({'INFO'}, f"Normalized {normalized} vertex(es)")
        return {'FINISHED'}


class UMA_OT_tools_normalize_all_weights(bpy.types.Operator):
    bl_idname = "uma_tools.normalize_all_weights"
    bl_label = "Normalize all"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object")
            return {'CANCELLED'}

        initial_mode = obj.mode
        all_indices = [v.index for v in obj.data.vertices]
        if not all_indices:
            self.report({'WARNING'}, "Active mesh has no vertices")
            return {'CANCELLED'}

        try:
            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            normalized, skipped = _normalize_vertex_weights_for_indices(obj, all_indices)

        except RuntimeError as e:
            self.report({'ERROR'}, f"Normalize all failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            if obj.mode != initial_mode:
                try:
                    bpy.ops.object.mode_set(mode=initial_mode)
                except Exception:
                    pass

        if skipped:
            self.report({'INFO'}, f"Normalized {normalized} vertex(es); skipped {skipped} without weights")
        else:
            self.report({'INFO'}, f"Normalized {normalized} vertex(es)")
        return {'FINISHED'}


class UMA_OT_tools_reset_pose_transforms(bpy.types.Operator):
    bl_idname = "uma_tools.reset_pose_transforms"
    bl_label = "Reset pose transforms"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armatures = [o for o in context.scene.objects if o is not None and o.type == 'ARMATURE']
        if not armatures:
            self.report({'WARNING'}, "No armatures found in the scene")
            return {'CANCELLED'}

        prev_active = context.view_layer.objects.active
        prev_selected = _get_selected_objects(context)
        prev_mode = prev_active.mode if prev_active else 'OBJECT'

        try:
            for arm in armatures:
                _deselect_all_objects(context)
                arm.select_set(True)
                context.view_layer.objects.active = arm

                if arm.mode != 'POSE':
                    bpy.ops.object.mode_set(mode='POSE')

                bpy.ops.pose.select_all(action='SELECT')
                bpy.ops.pose.transforms_clear()

        except RuntimeError as e:
            self.report({'ERROR'}, f"Reset pose transforms failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            _deselect_all_objects(context)
            for o in prev_selected:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

            if prev_active is not None:
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except Exception:
                    bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, f"Reset pose transforms on {len(armatures)} armature(s)")
        return {'FINISHED'}


class UMA_OT_tools_select_edge_loops(bpy.types.Operator):
    bl_idname = "uma_tools.select_edge_loops"
    bl_label = "Select edge loops"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object to use edge loop selection")
            return {'CANCELLED'}

        prev_mode = obj.mode
        try:
            if prev_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')

            bpy.ops.mesh.select_mode(type='EDGE')
            bpy.ops.mesh.loop_select()

        except RuntimeError as e:
            self.report({'ERROR'}, f"Select edge loops failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            if obj.mode != prev_mode:
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except Exception:
                    pass

        return {'FINISHED'}


class UMA_OT_tools_copy_weights_mirrored(bpy.types.Operator):
    bl_idname = "uma_tools.copy_weights_mirrored"
    bl_label = "Copy Weights Mirrored"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object to copy mirrored weights")
            return {'CANCELLED'}

        prev_mode = obj.mode
        try:
            if prev_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')

            bm = bmesh.from_edit_mesh(obj.data)
            selected_verts = [v for v in bm.verts if v.select]
            if not selected_verts:
                self.report({'WARNING'}, "No selected vertices found")
                return {'CANCELLED'}

            kd = kdtree.KDTree(len(bm.verts))
            for v in bm.verts:
                kd.insert(v.co, v.index)
            kd.balance()

            mirror_map = []
            for v in selected_verts:
                mirror_pos = v.co.copy()
                mirror_pos.x *= -1.0

                _co, mirror_index, dist = kd.find(mirror_pos)
                if dist > 1e-6:
                    mirror_map.append((v.index, None))
                else:
                    mirror_map.append((v.index, mirror_index))

            bpy.ops.object.mode_set(mode='OBJECT')

            mesh = obj.data
            copied = 0
            skipped = 0

            for src_index, mirror_index in mirror_map:
                if mirror_index is None:
                    skipped += 1
                    continue

                src_vert = mesh.vertices[src_index]
                for g in src_vert.groups:
                    src_group = obj.vertex_groups[g.group]
                    src_name = src_group.name
                    if src_name.startswith("Left"):
                        tgt_name = "Right" + src_name[4:]
                    elif src_name.startswith("Right"):
                        tgt_name = "Left" + src_name[5:]
                    else:
                        tgt_name = src_name

                    tgt_group = obj.vertex_groups.get(tgt_name)
                    if tgt_group is None:
                        tgt_group = obj.vertex_groups.new(name=tgt_name)

                    tgt_group.add([mirror_index], g.weight, 'REPLACE')

                copied += 1

        except RuntimeError as e:
            self.report({'ERROR'}, f"Copy mirrored weights failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            if obj.mode != prev_mode:
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except Exception:
                    pass

        if skipped > 0:
            self.report({'INFO'}, f"Copied weights for {copied} vertex(es); skipped {skipped} without a mirror")
        else:
            self.report({'INFO'}, f"Copied weights for {copied} vertex(es)")
        return {'FINISHED'}


class UMA_OT_tools_add_current_vertex_group(bpy.types.Operator):
    bl_idname = "uma_tools.add_current_vertex_group"
    bl_label = "Add current vertex group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object with an active vertex group")
            return {'CANCELLED'}

        vg = obj.vertex_groups.active
        if vg is None:
            self.report({'WARNING'}, "No active vertex group to add")
            return {'CANCELLED'}

        scene = context.scene
        for item in scene.uma_tools_group_quick_select:
            if item.name == vg.name:
                self.report({'INFO'}, f"'{vg.name}' already added")
                return {'CANCELLED'}

        new_item = scene.uma_tools_group_quick_select.add()
        new_item.name = vg.name
        scene.uma_tools_group_quick_select_index = len(scene.uma_tools_group_quick_select) - 1
        return {'FINISHED'}


class UMA_OT_tools_select_vertex_group(bpy.types.Operator):
    bl_idname = "uma_tools.select_vertex_group"
    bl_label = "Select"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: bpy.props.StringProperty(default="")

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object")
            return {'CANCELLED'}

        vg = obj.vertex_groups.get(self.group_name)
        if vg is None:
            self.report({'WARNING'}, f"Vertex group '{self.group_name}' not found")
            return {'CANCELLED'}

        obj.vertex_groups.active_index = vg.index

        try:
            bpy.ops.object.vertex_group_select()
        except RuntimeError:
            prev_mode = obj.mode
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.object.vertex_group_select()
            except RuntimeError:
                pass
            finally:
                if obj.mode != prev_mode:
                    try:
                        bpy.ops.object.mode_set(mode=prev_mode)
                    except Exception:
                        pass

        return {'FINISHED'}


class UMA_OT_tools_select_vertex_group_opposite(bpy.types.Operator):
    bl_idname = "uma_tools.select_vertex_group_opposite"
    bl_label = "Opposite"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: bpy.props.StringProperty(default="")

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object")
            return {'CANCELLED'}

        opposite_name = _swap_left_right_name(self.group_name)
        if opposite_name == self.group_name:
            self.report({'WARNING'}, "No Left/Right prefix to swap")
            return {'CANCELLED'}

        vg = obj.vertex_groups.get(opposite_name)
        if vg is None:
            self.report({'WARNING'}, f"Vertex group '{opposite_name}' not found")
            return {'CANCELLED'}

        obj.vertex_groups.active_index = vg.index

        return {'FINISHED'}


class UMA_OT_tools_select_all_vertices(bpy.types.Operator):
    bl_idname = "uma_tools.select_all_vertices"
    bl_label = "Select all vertexes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object")
            return {'CANCELLED'}

        prev_mode = obj.mode
        try:
            if prev_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
        except RuntimeError as e:
            self.report({'ERROR'}, f"Select all vertices failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            if obj.mode != prev_mode:
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except Exception:
                    pass

        return {'FINISHED'}


class UMA_OT_tools_unselect_all_vertices(bpy.types.Operator):
    bl_idname = "uma_tools.unselect_all_vertices"
    bl_label = "Unselect all vertexes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "Select a mesh object")
            return {'CANCELLED'}

        prev_mode = obj.mode
        try:
            if prev_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
        except RuntimeError as e:
            self.report({'ERROR'}, f"Unselect all vertices failed: {str(e)}")
            return {'CANCELLED'}
        finally:
            if obj.mode != prev_mode:
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except Exception:
                    pass

        return {'FINISHED'}


class UMA_OT_tools_cursor_move_to_origin(bpy.types.Operator):
    bl_idname = "uma_tools.cursor_move_to_origin"
    bl_label = "Move to Origin"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        context.scene.cursor.location = (0.0, 0.0, 0.0)
        return {'FINISHED'}


class UMA_OT_tools_cursor_align_with_object(bpy.types.Operator):
    bl_idname = "uma_tools.cursor_align_with_object"
    bl_label = "Align with Object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None:
            self.report({'WARNING'}, "Select an object to align the 3D Cursor")
            return {'CANCELLED'}

        context.scene.cursor.location = obj.matrix_world.translation
        return {'FINISHED'}


class UMA_OT_tools_remove_vertex_group_quick_select(bpy.types.Operator):
    bl_idname = "uma_tools.remove_vertex_group_quick_select"
    bl_label = "Remove"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        scene = context.scene
        if self.index < 0 or self.index >= len(scene.uma_tools_group_quick_select):
            return {'CANCELLED'}

        scene.uma_tools_group_quick_select.remove(self.index)
        scene.uma_tools_group_quick_select_index = min(
            scene.uma_tools_group_quick_select_index,
            max(0, len(scene.uma_tools_group_quick_select) - 1),
        )
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
        prev_sel = _get_selected_objects(context)

        try:
            for obj in targets:
                if obj is None:
                    continue

                if not _needs_transform_apply(obj):
                    continue

                # Blender ops require active + selected.
                _deselect_all_objects(context)
                obj.select_set(True)
                context.view_layer.objects.active = obj

                try:
                    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
                except RuntimeError:
                    # Ignore objects that can’t be applied due to mode/constraints
                    pass
        finally:
            # Restore selection
            _deselect_all_objects(context)
            for o in prev_sel:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

        # Re-run checks after fix
        bpy.ops.uma_tools.check_errors(selected_only=self.selected_only)
        return {'FINISHED'}


class UMA_OT_tools_fix_transform_from_report(bpy.types.Operator):
    bl_idname = "uma_tools.fix_transform_from_report"
    bl_label = "Fix transform"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(
        items=(
            ("SELECTED", "Selected", "Fix selected report item"),
            ("ALL", "All", "Fix all transform report items"),
        ),
        default="SELECTED",
    )

    def _apply_transform(self, context, obj):
        if obj is None or obj.type not in {'MESH', 'ARMATURE'}:
            return False
        if not _needs_transform_apply(obj):
            return False

        prev_active = context.view_layer.objects.active
        prev_sel = _get_selected_objects(context)
        try:
            _deselect_all_objects(context)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        except RuntimeError:
            return False
        finally:
            _deselect_all_objects(context)
            for o in prev_sel:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

        return True

    def execute(self, context):
        wm = context.window_manager
        lines = wm.uma_tools_report_lines

        if not lines:
            self.report({'WARNING'}, "No report items")
            return {'CANCELLED'}

        targets = []
        if self.mode == "SELECTED":
            if not (0 <= wm.uma_tools_report_index < len(lines)):
                self.report({'WARNING'}, "No report item selected")
                return {'CANCELLED'}
            name = _get_transform_object_name_from_line(lines[wm.uma_tools_report_index].text)
            if name:
                obj = context.scene.objects.get(name)
                if obj is not None:
                    targets.append(obj)
        else:
            seen = set()
            for item in lines:
                name = _get_transform_object_name_from_line(item.text)
                if name and name not in seen:
                    obj = context.scene.objects.get(name)
                    if obj is not None:
                        targets.append(obj)
                        seen.add(name)

        if not targets:
            self.report({'WARNING'}, "No transform items to fix")
            return {'CANCELLED'}

        fixed = 0
        for obj in targets:
            if self._apply_transform(context, obj):
                fixed += 1

        bpy.ops.uma_tools.check_errors(selected_only=False)
        self.report({'INFO'}, f"Fixed transforms on {fixed} object(s)")
        return {'FINISHED'}


class UMA_OT_tools_fix_missing_armature_modifiers(bpy.types.Operator):
    bl_idname = "uma_tools.fix_missing_armature_modifiers"
    bl_label = "Fix all missing Armature"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        wm = context.window_manager
        lines = wm.uma_tools_report_lines
        if not lines:
            self.report({'WARNING'}, "No report items")
            return {'CANCELLED'}

        root = context.scene.objects.get("Root")
        if root is None or root.type != 'ARMATURE':
            self.report({'ERROR'}, "Armature object named 'Root' not found")
            return {'CANCELLED'}

        targets = []
        seen = set()
        for item in lines:
            name = _get_missing_armature_object_name_from_line(item.text)
            if name and name not in seen:
                obj = context.scene.objects.get(name)
                if obj is not None and obj.type == 'MESH':
                    targets.append(obj)
                    seen.add(name)

        if not targets:
            self.report({'WARNING'}, "No missing Armature modifier items to fix")
            return {'CANCELLED'}

        fixed = 0
        for obj in targets:
            if _has_armature_modifier(obj):
                continue
            mod = obj.modifiers.new(name="Armature", type='ARMATURE')
            mod.object = root
            fixed += 1

        bpy.ops.uma_tools.check_errors(selected_only=False)
        self.report({'INFO'}, f"Added Armature modifier to {fixed} object(s)")
        return {'FINISHED'}


class UMA_OT_tools_select_all(bpy.types.Operator):
    bl_idname = "uma_tools.select_all"
    bl_label = "Select All"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _deselect_all_objects(context)
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
            bpy.ops.export_scene.fbx(filepath=self.filepath, **_uma_fbx_export_kwargs(context, use_selection=False))
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
            bpy.ops.export_scene.fbx(filepath=self.filepath, **_uma_fbx_export_kwargs(context, use_selection=True))
            self.report({'INFO'}, f"Exported FBX: {os.path.basename(self.filepath)}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            return {'CANCELLED'}

    def invoke(self, context, event):
        self.filepath = _uma_default_export_path("_selected")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class UMA_OT_tools_split_udims_to_textures(bpy.types.Operator):
    bl_idname = "uma_tools.split_udims_to_textures"
    bl_label = "Split UDIMS into separate textures"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        targets = [o for o in context.selected_objects if o is not None and o.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        prev_active = context.view_layer.objects.active
        prev_selected = _get_selected_objects(context)

        created_mats = 0
        reassigned_faces = 0
        converted_nodes = 0
        missing_tiles = 0
        multi_tile_faces = 0
        image_cache: dict[str, bpy.types.Image] = {}

        try:
            for obj in targets:
                if obj.data is None:
                    continue

                _ensure_object_mode(context, obj)

                uv_name = _get_object_active_uv_name(obj)
                if not uv_name:
                    self.report({'WARNING'}, f"'{obj.name}' has no UV layers; skipping")
                    continue
                uv_layer = obj.data.uv_layers.get(uv_name)
                if uv_layer is None:
                    continue

                polys = obj.data.polygons
                if not polys:
                    continue

                slot_materials = list(obj.data.materials)
                if not slot_materials:
                    continue

                # Cache: (orig_material_name, udim_number) -> slot_index
                new_slot_by_key: dict[tuple[str, int], int] = {}

                for slot_index, mat in enumerate(slot_materials):
                    if mat is None:
                        continue

                    tile_to_polys: dict[tuple[int, int], list[int]] = {}

                    for poly in polys:
                        if poly.material_index != slot_index:
                            continue

                        loop_uvs = []
                        for li in poly.loop_indices:
                            try:
                                loop_uvs.append(uv_layer.data[li].uv)
                            except Exception:
                                pass

                        if not loop_uvs:
                            continue

                        # Determine the face's UDIM tile from average UV.
                        u_avg = sum((uv.x for uv in loop_uvs), 0.0) / float(len(loop_uvs))
                        v_avg = sum((uv.y for uv in loop_uvs), 0.0) / float(len(loop_uvs))
                        tile_u, tile_v = _udim_tile_from_uv((u_avg, v_avg))

                        # Detect faces spanning multiple tiles (best-effort warning).
                        tiles_seen = set(_udim_tile_from_uv((uv.x, uv.y)) for uv in loop_uvs)
                        if len(tiles_seen) > 1:
                            multi_tile_faces += 1

                        tile_to_polys.setdefault((tile_u, tile_v), []).append(poly.index)

                    if not tile_to_polys:
                        continue

                    # Create per-tile materials and reassign faces.
                    for (tile_u, tile_v), poly_indices in tile_to_polys.items():
                        udim_number = _udim_number_from_tile(tile_u, tile_v)
                        key = (mat.name, udim_number)

                        if key not in new_slot_by_key:
                            clone_result = _clone_material_for_udim_tile(mat, udim_number, tile_u, tile_v, uv_name, image_cache)
                            if not clone_result:
                                continue

                            clone, cnv, miss = clone_result

                            obj.data.materials.append(clone)
                            new_slot_index = len(obj.data.materials) - 1
                            new_slot_by_key[key] = new_slot_index
                            created_mats += 1

                            converted_nodes += cnv
                            missing_tiles += miss

                        new_slot_index = new_slot_by_key[key]
                        for pi in poly_indices:
                            polys[pi].material_index = new_slot_index
                        reassigned_faces += len(poly_indices)

                obj.data.update()

        finally:
            _deselect_all_objects(context)
            for o in prev_selected:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

        msg = f"Created {created_mats} material(s); reassigned {reassigned_faces} face(s)"
        if converted_nodes:
            msg += f"; converted {converted_nodes} tiled image node(s)"
        if multi_tile_faces:
            msg += f"; {multi_tile_faces} face(s) spanned multiple tiles (best-effort assigned)"
        if missing_tiles:
            msg += f"; {missing_tiles} tiled image node(s) missing tile files"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class UMA_OT_tools_reset_to_udim(bpy.types.Operator):
    bl_idname = "uma_tools.reset_to_udim"
    bl_label = "Reset to UDIM"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        targets = [o for o in context.selected_objects if o is not None and o.type == 'MESH']
        if not targets:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        prev_active = context.view_layer.objects.active
        prev_selected = _get_selected_objects(context)

        restored_faces = 0
        removed_slots = 0
        deleted_materials = 0
        split_materials_to_delete = []

        try:
            for obj in targets:
                if obj.data is None:
                    continue

                _ensure_object_mode(context, obj)

                polys = obj.data.polygons
                if not polys:
                    continue

                # Build lookup of current slots -> materials
                materials = list(obj.data.materials)
                if not materials:
                    continue

                for mat in materials:
                    if mat is None:
                        continue
                    if bool(mat.get("uma_udim_split", False)) and mat not in split_materials_to_delete:
                        split_materials_to_delete.append(mat)

                # Ensure original materials exist in slots when needed.
                slot_index_by_mat_name: dict[str, int] = {}
                for idx, mat in enumerate(materials):
                    if mat is not None:
                        slot_index_by_mat_name[mat.name] = idx

                # Reassign faces from split clones back to original materials.
                for slot_index, mat in enumerate(materials):
                    if mat is None:
                        continue
                    if not bool(mat.get("uma_udim_split", False)):
                        continue

                    original_name = mat.get("uma_udim_original")
                    if not original_name:
                        continue
                    original_mat = bpy.data.materials.get(original_name)
                    if original_mat is None:
                        continue

                    if original_mat.name in slot_index_by_mat_name:
                        original_slot = slot_index_by_mat_name[original_mat.name]
                    else:
                        obj.data.materials.append(original_mat)
                        original_slot = len(obj.data.materials) - 1
                        slot_index_by_mat_name[original_mat.name] = original_slot

                    for poly in polys:
                        if poly.material_index == slot_index:
                            poly.material_index = original_slot
                            restored_faces += 1

                # Cleanup: remove unused slots (iterate in reverse and remap indices).
                used = [False] * len(obj.data.materials)
                for p in polys:
                    if 0 <= p.material_index < len(used):
                        used[p.material_index] = True

                for idx in range(len(obj.data.materials) - 1, -1, -1):
                    if used[idx]:
                        continue

                    obj.data.materials.pop(idx)
                    used.pop(idx)
                    removed_slots += 1

                    for p in polys:
                        if p.material_index > idx:
                            p.material_index -= 1

                obj.data.update()

            for mat in split_materials_to_delete:
                if mat is None:
                    continue
                if mat.name not in bpy.data.materials:
                    continue
                if getattr(mat, "users", 0) != 0:
                    continue
                try:
                    bpy.data.materials.remove(mat)
                    deleted_materials += 1
                except Exception:
                    pass

        finally:
            _deselect_all_objects(context)
            for o in prev_selected:
                if o and o.name in context.scene.objects:
                    o.select_set(True)
            context.view_layer.objects.active = prev_active

        self.report(
            {'INFO'},
            f"Restored {restored_faces} face(s); removed {removed_slots} unused slot(s); deleted {deleted_materials} split material(s)",
        )
        return {'FINISHED'}


class UMA_PT_tools_panel(bpy.types.Panel):
    bl_label = "UMA Tools"
    bl_idname = "UMA_PT_tools_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'UMA Tools'

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        scene = context.scene

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
            row = col.row(align=True)
            row.operator(UMA_OT_tools_fix_missing_armature_modifiers.bl_idname, text="Fix all missing Armature")
            op = row.operator(UMA_OT_tools_fix_transform_from_report.bl_idname, text="Fix all transform items")
            op.mode = "ALL"
            if 0 <= wm.uma_tools_report_index < len(wm.uma_tools_report_lines):
                sel = wm.uma_tools_report_lines[wm.uma_tools_report_index]
                col.separator()
                col.label(text="Selected:")
                col.label(text=getattr(sel, "text", ""))

        # Rigging and Weights
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_copy_weights", "Rigging and Weights")
        if wm.uma_tools_section_copy_weights:
            col = box.column(align=True)
            col.operator(UMA_OT_tools_reset_pose_transforms.bl_idname, text="Reset pose transforms")
            col.operator(UMA_OT_tools_copy_weights_mirrored.bl_idname, text="Copy Weights Mirrored")
            col.operator(UMA_OT_tools_remove_negligible_weights.bl_idname, text="Remove negligible weights")
            col.separator()
            col.prop(wm, "uma_tools_weights_source", text="Source")
            col.prop(wm, "uma_tools_smooth_weights", text="Smooth weights")
            col.prop(wm, "uma_tools_weight_mapping", text="Mapping")
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
            util.operator(UMA_OT_tools_normalize_selected_weights.bl_idname, text="Normalize Selected")
            util.operator(UMA_OT_tools_normalize_all_weights.bl_idname, text="Normalize All")

        # Editing Tools
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_editing_tools", "Editing Tools")
        if wm.uma_tools_section_editing_tools:
            edit = box.column(align=True)
            edit.operator(UMA_OT_tools_select_edge_loops.bl_idname, text="Select edge loops")

        # Vertex Group Quick Select
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_vertex_group_quick_select", "Vertex Group Quick Select")
        if wm.uma_tools_section_vertex_group_quick_select:
            vg = box.column(align=True)
            row = vg.row(align=True)
            row.operator(UMA_OT_tools_select_all_vertices.bl_idname, text="Select all vertexes")
            row.operator(UMA_OT_tools_unselect_all_vertices.bl_idname, text="Unselect all vertexes")
            vg.operator(UMA_OT_tools_add_current_vertex_group.bl_idname, text="Add current vertex group")
            vg.template_list(
                "UMA_UL_tools_vertex_group_quick_select",
                "",
                scene,
                "uma_tools_group_quick_select",
                scene,
                "uma_tools_group_quick_select_index",
                rows=6,
            )

        # 3D Cursor
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_3d_cursor", "3D Cursor")
        if wm.uma_tools_section_3d_cursor:
            cur = box.column(align=True)
            cur.operator(UMA_OT_tools_cursor_move_to_origin.bl_idname, text="Move to Origin")
            cur.operator(UMA_OT_tools_cursor_align_with_object.bl_idname, text="Align with Object")

        # UDIM Tools
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_udim_tools", "UDIM Tools")
        if wm.uma_tools_section_udim_tools:
            udim = box.column(align=True)
            udim.operator(UMA_OT_tools_split_udims_to_textures.bl_idname, text="Split UDIMS into separate textures")
            udim.operator(UMA_OT_tools_reset_to_udim.bl_idname, text="Reset to UDIM")

        # UMA Export
        box = layout.box()
        _draw_fold_header(box, "uma_tools_section_export", "UMA Export")
        if wm.uma_tools_section_export:
            box.prop(wm, "uma_tools_export_uma2_format", text="UMA 2 Format")
            box.operator_context = 'INVOKE_DEFAULT'
            box.operator(UMA_OT_tools_fbx_export_all.bl_idname, text="Export FBX (All)")
            box.operator(UMA_OT_tools_fbx_export_selected.bl_idname, text="Export FBX (Selected)")
            box.operator_context = 'EXEC_DEFAULT'

class UMA_ToolsReportLine(bpy.types.PropertyGroup):
    text: bpy.props.StringProperty(default="")


class UMA_ToolsVertexGroupQuickSelectItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(default="")


class UMA_UL_tools_report(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=getattr(item, "text", ""), icon='ERROR')
        else:
            layout.label(text="")


class UMA_UL_tools_vertex_group_quick_select(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=getattr(item, "name", ""))
            button_row = row.row(align=True)
            button_row.scale_x = 0.7
            op = button_row.operator(UMA_OT_tools_select_vertex_group.bl_idname, text="Select")
            op.group_name = getattr(item, "name", "")
            op = button_row.operator(UMA_OT_tools_select_vertex_group_opposite.bl_idname, text="Opposite")
            op.group_name = getattr(item, "name", "")
            op = row.operator(UMA_OT_tools_remove_vertex_group_quick_select.bl_idname, text="", icon='X')
            op.index = index
        else:
            layout.label(text="")


classes = (
    UMA_ToolsReportLine,
    UMA_ToolsVertexGroupQuickSelectItem,
    UMA_UL_tools_report,
    UMA_UL_tools_vertex_group_quick_select,
    UMA_OT_tools_check_errors,
    UMA_OT_tools_insert_global_position_bones,
    UMA_OT_tools_select_all,
    UMA_OT_tools_fix_transforms,
    UMA_OT_tools_fix_transform_from_report,
    UMA_OT_tools_fix_missing_armature_modifiers,
    UMA_OT_tools_copy_weights_to_selected,
    UMA_OT_tools_process_rename_selected,
    UMA_OT_tools_remove_empty_vertex_groups,
    UMA_OT_tools_remove_negligible_weights,
    UMA_OT_tools_normalize_selected_weights,
    UMA_OT_tools_normalize_all_weights,
    UMA_OT_tools_reset_pose_transforms,
    UMA_OT_tools_select_edge_loops,
    UMA_OT_tools_copy_weights_mirrored,
    UMA_OT_tools_add_current_vertex_group,
    UMA_OT_tools_select_vertex_group,
    UMA_OT_tools_select_vertex_group_opposite,
    UMA_OT_tools_select_all_vertices,
    UMA_OT_tools_unselect_all_vertices,
    UMA_OT_tools_cursor_move_to_origin,
    UMA_OT_tools_cursor_align_with_object,
    UMA_OT_tools_remove_vertex_group_quick_select,
    UMA_OT_tools_fbx_export_all,
    UMA_OT_tools_fbx_export_selected,
    UMA_OT_tools_split_udims_to_textures,
    UMA_OT_tools_reset_to_udim,
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

    if hasattr(bpy.types.Scene, "uma_tools_group_quick_select"):
        try:
            del bpy.types.Scene.uma_tools_group_quick_select
        except Exception:
            pass
    if hasattr(bpy.types.Scene, "uma_tools_group_quick_select_index"):
        try:
            del bpy.types.Scene.uma_tools_group_quick_select_index
        except Exception:
            pass

    bpy.types.Scene.uma_tools_group_quick_select = bpy.props.CollectionProperty(
        type=UMA_ToolsVertexGroupQuickSelectItem
    )
    bpy.types.Scene.uma_tools_group_quick_select_index = bpy.props.IntProperty(
        default=0,
        update=_on_quick_select_index_update,
    )

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

    if hasattr(bpy.types.WindowManager, "uma_tools_smooth_weights"):
        try:
            del bpy.types.WindowManager.uma_tools_smooth_weights
        except Exception:
            pass
    bpy.types.WindowManager.uma_tools_smooth_weights = bpy.props.BoolProperty(
        name="Smooth weights",
        description="Blend copied vertex weights with neighboring vertices using gentle smoothing",
        default=False,
    )

    if hasattr(bpy.types.WindowManager, "uma_tools_weight_mapping"):
        try:
            del bpy.types.WindowManager.uma_tools_weight_mapping
        except Exception:
            pass
    bpy.types.WindowManager.uma_tools_weight_mapping = bpy.props.EnumProperty(
        name="Mapping",
        description="Vertex mapping mode used by the Data Transfer modifier when copying weights",
        items=[
            ('TOPOLOGY', "Topology", "Copy from identical topology meshes"),
            ('NEAREST', "Nearest Vertex", "Copy from closest vertex"),
            ('EDGE_NEAREST', "Nearest Edge Vertex", "Copy from closest vertex of closest edge"),
            ('EDGEINTERP_NEAREST', "Nearest Edge Interpolated", "Copy from interpolated values of vertices from closest point on closest edge"),
            ('POLY_NEAREST', "Nearest Face Vertex", "Copy from closest vertex of closest face"),
            ('POLYINTERP_NEAREST', "Nearest Face Interpolated", "Copy from interpolated values of vertices from closest point on closest face"),
            ('POLYINTERP_VNORPROJ', "Projected Face Interpolated", "Copy from interpolated values of vertices from point on closest face hit by normal-projection"),
        ],
        default='POLYINTERP_NEAREST',
    )

    if hasattr(bpy.types.WindowManager, "uma_tools_export_uma2_format"):
        try:
            del bpy.types.WindowManager.uma_tools_export_uma2_format
        except Exception:
            pass
    bpy.types.WindowManager.uma_tools_export_uma2_format = bpy.props.BoolProperty(
        name="UMA 2 Format",
        description="Use UMA 2 FBX export axes for both export buttons",
        default=False,
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
        ("uma_tools_section_utilities", True),
        ("uma_tools_section_editing_tools", False),
        ("uma_tools_section_vertex_group_quick_select", False),
        ("uma_tools_section_3d_cursor", False),
        ("uma_tools_section_udim_tools", False),
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

    if hasattr(bpy.types.Scene, "uma_tools_group_quick_select"):
        try:
            del bpy.types.Scene.uma_tools_group_quick_select
        except Exception:
            pass
    if hasattr(bpy.types.Scene, "uma_tools_group_quick_select_index"):
        try:
            del bpy.types.Scene.uma_tools_group_quick_select_index
        except Exception:
            pass

    if hasattr(bpy.types.WindowManager, "uma_tools_weights_source"):
        try:
            del bpy.types.WindowManager.uma_tools_weights_source
        except Exception:
            pass

    if hasattr(bpy.types.WindowManager, "uma_tools_smooth_weights"):
        try:
            del bpy.types.WindowManager.uma_tools_smooth_weights
        except Exception:
            pass

    if hasattr(bpy.types.WindowManager, "uma_tools_weight_mapping"):
        try:
            del bpy.types.WindowManager.uma_tools_weight_mapping
        except Exception:
            pass

    if hasattr(bpy.types.WindowManager, "uma_tools_export_uma2_format"):
        try:
            del bpy.types.WindowManager.uma_tools_export_uma2_format
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
        "uma_tools_section_editing_tools",
        "uma_tools_section_vertex_group_quick_select",
        "uma_tools_section_3d_cursor",
        "uma_tools_section_udim_tools",
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
