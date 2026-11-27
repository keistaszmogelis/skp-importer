"""Import SketchUp files through Trimble's standalone desktop SDK."""

bl_info = {
    "name": "SketchUp (.skp)",
    "version": (0, 2, 24),
    "blender": (4, 2, 0),
    "location": "File > Import",
    "category": "Import-Export",
}

import ctypes as C
import os
import re
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector


class Ref(C.Structure):
    _fields_ = [("ptr", C.c_void_p)]


class Point(C.Structure):
    _fields_ = [(axis, C.c_double) for axis in "xyz"]

    def tuple(self):
        return self.x, self.y, self.z


class Color(C.Structure):
    _fields_ = [(channel, C.c_ubyte) for channel in ("red", "green", "blue", "alpha")]


class Transform(C.Structure):
    _fields_ = [("values", C.c_double * 16)]


class SDK:
    def __init__(self, path):
        if sys.platform not in {"win32", "darwin"}:
            raise RuntimeError("This add-on requires Windows x64 or macOS.")
        path = Path(path)
        if path.is_dir() and path.suffix == ".framework":
            path = path / "SketchUpAPI"
        if not path.is_file():
            raise RuntimeError("SketchUp SDK runtime missing. Install a ZIP with the runtime for your platform.")
        if sys.platform == "win32":
            with os.add_dll_directory(str(path.parent)):
                self.dll = C.CDLL(str(path))
        else:
            self.dll = C.CDLL(str(path))
        self.dll.SUInitialize.argtypes = []
        self.dll.SUInitialize.restype = None
        self.dll.SUTerminate.argtypes = []
        self.dll.SUTerminate.restype = None

    def call(self, name, *args, optional=False):
        function = getattr(self.dll, name)
        function.argtypes = [type(arg) for arg in args]
        function.restype = C.c_int
        result = self.invoke(function, *args)
        if result == 9 and optional:
            return False
        if result:
            raise RuntimeError(f"{name} returned SketchUp error {result}")
        return True

    @staticmethod
    def invoke(function, *args):
        try:
            return function(*args)
        except OSError as error:
            raise RuntimeError(f"SketchUp SDK call {function.__name__} failed: {error}") from error

    def value(self, name, ref, kind=Ref, optional=False):
        output = kind()
        if self.call(name, ref, C.pointer(output), optional=optional):
            return output
        return None

    def array(self, name, ref, count, kind=Ref, *, query_args=()):
        if not count:
            return []
        output = (kind * count)()
        written = C.c_size_t()
        self.call(name, ref, *query_args, C.c_size_t(count),
                  C.cast(output, C.POINTER(kind)), C.pointer(written))
        return list(output[:written.value])

    def items(self, prefix, suffix, ref):
        count = self.value(f"{prefix}GetNum{suffix}", ref, C.c_size_t)
        return self.array(f"{prefix}Get{suffix}", ref, count.value)

    def entity_edge_count(self, entities):
        # False includes loose edges, curves, and edges belonging to faces.
        standalone_only = C.c_bool(False)
        count = C.c_size_t()
        self.call("SUEntitiesGetNumEdges", entities, standalone_only, C.pointer(count))
        return count.value

    def entity_edges(self, entities):
        return self.array("SUEntitiesGetEdges", entities, self.entity_edge_count(entities),
                          query_args=(C.c_bool(False),))

    def string(self, name, ref):
        string = Ref()
        self.call("SUStringCreate", C.pointer(string))
        try:
            self.call(name, ref, C.pointer(string))
            length = self.value("SUStringGetUTF8Length", string, C.c_size_t).value
            buffer = C.create_string_buffer(length + 1)
            written = C.c_size_t()
            self.call("SUStringGetUTF8", string, C.c_size_t(length + 1), C.cast(buffer, C.POINTER(C.c_char)), C.pointer(written))
            return buffer.value.decode("utf-8", errors="replace")
        finally:
            self.call("SUStringRelease", C.pointer(string))


INCH = 0.0254
IDENTITY = Matrix.Identity(4)


def position(point):
    return Vector(point.tuple()) * INCH


def component_matrix(transform):
    values = transform.values
    matrix = Matrix(tuple(tuple(values[column * 4 + row] for column in range(4)) for row in range(4)))
    # SketchUp's homogeneous scale can be stored in the final element.
    scale = matrix[3][3] or 1.0
    matrix = matrix * (1.0 / scale)
    matrix.translation *= INCH
    return matrix


def uv(coordinate):
    q = coordinate.z
    return (coordinate.x / q, coordinate.y / q) if abs(q) > 1e-12 else (0.0, 0.0)


def linear(channel):
    channel /= 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def merge_face_triangles(triangles):
    """Dissolve shared edges while each merged region has one simple boundary."""
    regions = {}
    owners = {}
    for index, triangle in enumerate(triangles):
        boundary = set(zip(triangle, triangle[1:] + triangle[:1]))
        while True:
            neighbors = {owners[(end, start)] for start, end in boundary if (end, start) in owners}
            for neighbor in sorted(neighbors):
                other = regions[neighbor]
                combined = boundary | other
                merged = {edge for edge in combined if (edge[1], edge[0]) not in combined}
                if polygon_boundary(merged) is None:
                    continue
                for edge in regions.pop(neighbor):
                    del owners[edge]
                boundary = merged
                break
            else:
                break
        regions[index] = boundary
        owners.update((edge, index) for edge in boundary)
    return [polygon_boundary(boundary) for boundary in regions.values()]


def polygon_boundary(edges):
    if len(edges) < 3:
        return None
    following = dict(edges)
    if len(following) != len(edges):
        return None
    start = min(following)
    ring = []
    visited = set()
    vertex = start
    while vertex in following and vertex not in visited:
        visited.add(vertex)
        ring.append(vertex)
        vertex = following[vertex]
    if vertex == start and len(ring) == len(edges):
        return ring
    return None


def make_glass_group():
    group = bpy.data.node_groups.new("Paulius GLASS", "ShaderNodeTree")
    nodes, links = group.nodes, group.links
    shader = nodes.new("ShaderNodeBsdfPrincipled")
    shader.location = (0, 300)
    shader.inputs["Alpha"].default_value = 1.0
    shader.inputs["Transmission Weight"].default_value = 1.0

    controls = (
        ("Base Color", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0), "Base Color", None),
        ("Roughness", "NodeSocketFloat", 0.0, "Roughness", (0.0, 1.0)),
        ("IOR", "NodeSocketFloat", 1.5, "IOR", (1.0, 100.0)),
        ("EEVEE Alpha", "NodeSocketFloat", 1.0, None, (0.0, 1.0)),
        ("Thin Wall", "NodeSocketBool", False, "Thin Wall", None),
        ("Dispersion Scale", "NodeSocketFloat", 0.0, "Transmission Dispersion Scale", (0.0, 1.0)),
        ("Dispersion Abbe Number", "NodeSocketFloat", 20.0, "Transmission Dispersion Abbe Number", (9.0, 91.0)),
        ("Shadow", "NodeSocketBool", False, None, None),
    )
    for name, socket_type, default, shader_input, limits in controls:
        socket = group.interface.new_socket(name=name, in_out="INPUT", socket_type=socket_type)
        socket.default_value = default
        if limits is not None:
            socket.min_value, socket.max_value = limits
        if shader_input and shader.inputs.get(shader_input) is None:
            socket.description = "Requires a Blender version with this Principled BSDF input"
    for name in ("Eevee", "Cycles"):
        group.interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketShader")

    inputs = nodes.new("NodeGroupInput")
    inputs.location = (-660, 300)
    outputs = nodes.new("NodeGroupOutput")
    outputs.location = (740, 300)
    for name, _, _, shader_input, _ in controls:
        if shader_input and shader.inputs.get(shader_input) is not None:
            links.new(inputs.outputs[name], shader.inputs[shader_input])

    transparent = nodes.new("ShaderNodeBsdfTransparent")
    transparent.location = (250, 20)
    light_path = nodes.new("ShaderNodeLightPath")
    light_path.location = (-660, -60)
    visible_ray = nodes.new("ShaderNodeMath")
    visible_ray.operation = "MAXIMUM"
    visible_ray.label = "Camera or Transmission"
    visible_ray.location = (-410, -60)
    links.new(light_path.outputs["Is Camera Ray"], visible_ray.inputs[0])
    links.new(light_path.outputs["Is Transmission Ray"], visible_ray.inputs[1])

    cycles_factor = nodes.new("ShaderNodeMath")
    cycles_factor.operation = "MAXIMUM"
    cycles_factor.label = "Visible Ray or Shadow Enabled"
    cycles_factor.location = (-190, -60)
    links.new(visible_ray.outputs[0], cycles_factor.inputs[0])
    links.new(inputs.outputs["Shadow"], cycles_factor.inputs[1])

    for name, factor, y in (("Eevee", inputs.outputs["EEVEE Alpha"], 300),
                            ("Cycles", cycles_factor.outputs[0], 20)):
        mix = nodes.new("ShaderNodeMixShader")
        mix.label = name
        mix.location = (480, y)
        links.new(factor, mix.inputs[0])
        links.new(transparent.outputs[0], mix.inputs[1])
        links.new(shader.outputs[0], mix.inputs[2])
        links.new(mix.outputs[0], outputs.inputs[name])
    return group


def glass_shader(material, group, rgba):
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.remove(nodes.get("Principled BSDF"))
    shader = nodes.new("ShaderNodeGroup")
    shader.node_tree = group
    shader.width = 240
    shader.inputs["Base Color"].default_value = (*rgba[:3], 1.0)
    shader.inputs["EEVEE Alpha"].default_value = rgba[3]

    eevee_output = nodes.get("Material Output")
    cycles_output = nodes.new("ShaderNodeOutputMaterial")
    for output, engine, socket, y in ((eevee_output, "EEVEE", "Eevee", 240),
                                      (cycles_output, "CYCLES", "Cycles", 0)):
        output.target = engine
        output.is_active_output = True
        output.location = (320, y)
        links.new(shader.outputs[socket], output.inputs["Surface"])
    return shader


class Geometry:
    def __init__(self):
        self.vertices = []
        self.edges = set()
        self.faces = []
        self.corners = []
        self.materials = []
        self.sharp = set()


class Importer:
    def __init__(self, sdk, collection, directory, *, create_bounds,
                 auto_join, threshold, import_materials, import_textures, import_joined_mesh=False,
                 hierarchy_levels=1, axes_size=0.2, progress=None):
        self.sdk = sdk
        self.collection = collection
        self.directory = Path(directory)
        self.import_hierarchy = hierarchy_levels > 0
        self.hierarchy_levels = hierarchy_levels
        self.axes_size = axes_size
        self.progress = progress
        self.import_joined_mesh = import_joined_mesh
        self.auto_join = self.import_hierarchy and auto_join
        self.import_materials = import_materials
        self.import_textures = import_materials and import_textures
        self.threshold = threshold
        self.create_bounds = self.import_hierarchy and create_bounds
        self.materials = {}
        self.texture_scales = {}
        self.objects = []
        self.meshes = []
        self.component_meshes = {}
        self.mesh_part_counts = {}
        self.child_cache = {}
        self.component_counts = {}
        self.images = []
        self.new_materials = []
        self.glass_group = None

    def material(self, ref):
        if ref is None:
            return None
        sdk = self.sdk
        if ref.ptr in self.materials:
            return self.materials[ref.ptr]
        name = sdk.string("SUMaterialGetName", ref) or "SketchUp Material"
        material = bpy.data.materials.new(name)
        self.new_materials.append(material)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        shader = nodes.get("Principled BSDF")
        color = sdk.value("SUMaterialGetColor", ref, Color)
        opacity = sdk.value("SUMaterialGetOpacity", ref, C.c_double).value
        rgba = (linear(color.red), linear(color.green), linear(color.blue), opacity)
        material.diffuse_color = rgba
        shader.inputs["Base Color"].default_value = rgba
        shader.inputs["Alpha"].default_value = opacity
        if re.search(r"\bglass\b", name.replace("_", " "), re.IGNORECASE):
            if self.glass_group is None:
                self.glass_group = make_glass_group()
            shader = glass_shader(material, self.glass_group, rgba)
        texture = sdk.value("SUMaterialGetTexture", ref, optional=True) if self.import_textures else None
        if texture:
            width, height = C.c_size_t(), C.c_size_t()
            u_scale, v_scale = C.c_double(), C.c_double()
            sdk.call("SUTextureGetDimensions", texture, C.pointer(width), C.pointer(height),
                     C.pointer(u_scale), C.pointer(v_scale))
            self.texture_scales[ref.ptr] = (u_scale.value, v_scale.value)
            filename = sdk.string("SUTextureGetFileName", texture)
            extension = Path(filename).suffix.lower()
            if extension not in (".jpg", ".jpeg", ".png", ".bmp", ".tga", ".tif", ".tiff", ".gif", ".webp"):
                extension = ".png"
            path = self.directory / f"texture-{len(self.materials)}{extension}"
            sdk.call("SUTextureWriteToFile", texture, C.c_char_p(str(path).encode("utf-8")))
            image = bpy.data.images.load(str(path), check_existing=False)
            self.images.append(image)
            image.name = Path(filename).name or path.name
            image.pack()
            node = nodes.new("ShaderNodeTexImage")
            node.image = image
            node.location = (-320, 300)
            material.node_tree.links.new(node.outputs["Color"], shader.inputs["Base Color"])
        self.materials[ref.ptr] = material
        return material

    def face_data(self, face, inherited):
        sdk = self.sdk
        front = sdk.value("SUFaceGetFrontMaterial", face, optional=True)
        back = sdk.value("SUFaceGetBackMaterial", face, optional=True)
        chosen = front or back or inherited
        material = self.material(chosen) if self.import_materials else None
        texture_scale = self.texture_scales.get(chosen.ptr) if chosen is not None else None
        reverse = front is None and back is not None
        helper = Ref()
        sdk.call("SUMeshHelperCreate", C.pointer(helper), face)
        try:
            size = sdk.value("SUMeshHelperGetNumVertices", helper, C.c_size_t).value
            points = sdk.array("SUMeshHelperGetVertices", helper, size, Point)
            normals = sdk.array("SUMeshHelperGetNormals", helper, size, Point)
            coords = []
            if texture_scale:
                side = "Back" if reverse else "Front"
                coords = sdk.array(f"SUMeshHelperGet{side}STQCoords", helper, size, Point)
            lookup = {p.tuple(): i for i, p in enumerate(points)}
            hole_count = sdk.value("SUFaceGetNumInnerLoops", face, C.c_size_t).value
            normal = sdk.value("SUFaceGetNormal", face, Point).tuple()
            if hole_count:
                count = sdk.value("SUMeshHelperGetNumTriangles", helper, C.c_size_t).value
                indices = sdk.array("SUMeshHelperGetVertexIndices", helper, count * 3, C.c_size_t)
                triangles = []
                for offset in range(0, len(indices), 3):
                    triangle = [lookup[points[index].tuple()] for index in indices[offset:offset + 3]]
                    if len(set(triangle)) != 3:
                        continue
                    a, b, c = (Vector(points[index].tuple()) for index in triangle)
                    if (b - a).cross(c - a).dot(Vector(normal)) < 0:
                        triangle.reverse()
                    triangles.append(triangle)
                rings = triangles if hole_count >= 12 else merge_face_triangles(triangles)
                polygons = [[points[index] for index in ring] for ring in rings]
            else:
                outer = sdk.value("SUFaceGetOuterLoop", face)
                polygons = [[sdk.value("SUVertexGetPosition", vertex, Point)
                             for vertex in sdk.items("SULoop", "Vertices", outer)]]
            corners = []
            for polygon in polygons:
                data = []
                for point in polygon:
                    index = lookup.get(point.tuple())
                    point_normal = normals[index].tuple() if index is not None and index < len(normals) else normal
                    texcoord = uv(coords[index]) if index is not None and index < len(coords) else (0.0, 0.0)
                    if texture_scale and front is None and back is None:
                        texcoord = tuple(value * scale for value, scale in zip(texcoord, texture_scale))
                    data.append((point, point_normal, texcoord))
                if reverse:
                    data = [(point, tuple(-v for v in normal), texcoord)
                            for point, normal, texcoord in reversed(data)]
                corners.append(data)
            return material, corners
        finally:
            sdk.call("SUMeshHelperRelease", C.pointer(helper))

    def append_entities(self, geometry, entities, transform, inherited):
        sdk = self.sdk
        vertices = {}
        normal_matrix = transform.to_3x3().inverted_safe().transposed()
        mirrored = transform.to_3x3().determinant() < 0

        def vertex_index(point):
            key = point.tuple()
            if key not in vertices:
                vertices[key] = len(geometry.vertices)
                geometry.vertices.append(transform @ position(point))
            return vertices[key]

        def edge_key(edge):
            start = sdk.value("SUEdgeGetStartVertex", edge)
            end = sdk.value("SUEdgeGetEndVertex", edge)
            endpoints = [vertex_index(sdk.value("SUVertexGetPosition", vertex, Point))
                         for vertex in (start, end)]
            return tuple(sorted(endpoints))

        for edge in sdk.entity_edges(entities):
            key = edge_key(edge)
            if key[0] != key[1]:
                geometry.edges.add(key)

        for face in sdk.items("SUEntities", "Faces", entities):
            material, polygons = self.face_data(face, inherited)
            smooth = False
            for edge in sdk.items("SUFace", "Edges", face):
                edge_smooth = sdk.value("SUEdgeGetSmooth", edge, C.c_bool).value
                smooth |= edge_smooth
                if not edge_smooth:
                    geometry.sharp.add(edge_key(edge))
            for polygon in polygons:
                if mirrored:
                    polygon.reverse()
                indices = [vertex_index(point) for point, _, _ in polygon]
                if len(set(indices)) != len(indices) or len(indices) < 3:
                    continue
                geometry.faces.append(indices)
                geometry.materials.append((material, smooth))
                geometry.corners.extend((normal_matrix @ Vector(normal), texcoord)
                                        for _, normal, texcoord in polygon)

    def children(self, entities):
        if entities.ptr not in self.child_cache:
            self.child_cache[entities.ptr] = tuple(self.read_children(entities))
        return self.child_cache[entities.ptr]

    def read_children(self, entities):
        sdk = self.sdk
        for group in sdk.items("SUEntities", "Groups", entities):
            yield (sdk.string("SUGroupGetName", group) or "Group",
                   sdk.value("SUGroupGetEntities", group),
                   component_matrix(sdk.value("SUGroupGetTransform", group, Transform)),
                   self.instance_material("SUGroupToDrawingElement", group))
        for instance in sdk.items("SUEntities", "Instances", entities):
            definition = sdk.value("SUComponentInstanceGetDefinition", instance)
            name = sdk.string("SUComponentInstanceGetName", instance)
            yield (name or sdk.string("SUComponentDefinitionGetName", definition) or "Component",
                   sdk.value("SUComponentDefinitionGetEntities", definition),
                   component_matrix(sdk.value("SUComponentInstanceGetTransform", instance, Transform)),
                   self.instance_material("SUComponentInstanceToDrawingElement", instance))

    def instance_material(self, conversion, instance):
        if not self.import_materials:
            return None
        function = getattr(self.sdk.dll, conversion)
        function.argtypes = [Ref]
        function.restype = Ref
        return self.sdk.value("SUDrawingElementGetMaterial", self.sdk.invoke(function, instance), optional=True)

    def mesh_from_geometry(self, name, geometry):
        mesh = bpy.data.meshes.new(name)
        self.meshes.append(mesh)
        # from_pydata expects unique edges, including those it derives from polygons.
        polygon_edges = {tuple(sorted((face[index - 1], vertex)))
                         for face in geometry.faces for index, vertex in enumerate(face)}
        mesh.from_pydata(geometry.vertices, sorted(geometry.edges - polygon_edges),
                         geometry.faces, shade_flat=False)
        mesh.update()
        slots = {}
        if geometry.faces:
            layer = mesh.uv_layers.new(name="UVMap")
            layer.uv.foreach_set("vector", [v for _, pair in geometry.corners for v in pair])
        for polygon, (material, smooth) in zip(mesh.polygons, geometry.materials):
            polygon.use_smooth = smooth
            if not self.import_materials:
                continue
            if material not in slots:
                slots[material] = len(mesh.materials)
                mesh.materials.append(material)
            polygon.material_index = slots[material]
        for edge in mesh.edges:
            edge.use_edge_sharp = tuple(sorted(edge.vertices)) in geometry.sharp
        if geometry.corners:
            mesh.normals_split_custom_set([normal.normalized() for normal, _ in geometry.corners])
        return mesh

    def create_object(self, name, mesh, parent, transform):
        obj = bpy.data.objects.new(name, mesh)
        self.objects.append(obj)
        self.collection.objects.link(obj)
        obj.parent = parent
        obj.matrix_basis = transform
        return obj

    def collect(self, geometry, entities, transform, inherited, children):
        self.append_entities(geometry, entities, transform, inherited)
        for _, child_entities, child_matrix, material in children:
            yield from self.collect(geometry, child_entities, transform @ child_matrix,
                         material or inherited, self.children(child_entities))
            if self.progress:
                self.progress.component_done()
            yield

    def has_geometry(self, entities):
        return bool(self.sdk.value("SUEntitiesGetNumFaces", entities, C.c_size_t).value
                    or self.sdk.entity_edge_count(entities))

    def count_mesh_parts(self, entities):
        if entities.ptr not in self.mesh_part_counts:
            count = int(self.has_geometry(entities))
            for _, child_entities, _, _ in self.children(entities):
                count += self.count_mesh_parts(child_entities)
                if count >= self.threshold:
                    break
            self.mesh_part_counts[entities.ptr] = count
        return self.mesh_part_counts[entities.ptr]

    def component(self, name, entities, transform, inherited, parent, depth=0):
        start = len(self.objects)
        children = list(self.children(entities))
        if len(children) == 1 and not self.has_geometry(entities):
            child_name, child_entities, child_transform, material = children[0]
            obj = yield from self.component(child_name, child_entities, transform @ child_transform,
                                 material or inherited, parent, depth)
            if self.progress:
                self.progress.component_done()
            return obj
        location, rotation, scale = transform.decompose()
        object_transform = Matrix.LocRotScale(location, rotation, scale)
        residual = object_transform.inverted_safe() @ transform
        if all(abs(value - IDENTITY[row][column]) <= 1e-6
               for row, values in enumerate(residual)
               for column, value in enumerate(values)):
            residual = IDENTITY

        join = False
        if self.auto_join:
            if self.import_hierarchy and depth >= self.hierarchy_levels - 1:
                count = 0
                for _, child_entities, _, _ in children:
                    count += self.count_mesh_parts(child_entities)
                    if count >= self.threshold:
                        join = True
                        break
            else:
                join = len(children) >= self.threshold
        key = (entities.ptr, inherited.ptr if inherited else None, join,
               tuple(tuple(row) for row in residual))
        if key not in self.component_meshes:
            geometry = Geometry()
            if join:
                yield from self.collect(geometry, entities, residual, inherited, children)
            else:
                self.append_entities(geometry, entities, residual, inherited)
            self.component_meshes[key] = (
                self.mesh_from_geometry(name, geometry) if geometry.faces or geometry.edges else None
            )
        elif join and self.progress and self.progress.visible:
            self.progress.component_done(self.component_counts[entities.ptr])
        mesh = self.component_meshes[key]
        obj = None
        if mesh is not None or self.import_hierarchy:
            obj = self.create_object(name, mesh, parent, object_transform)
            if mesh is None:
                obj.empty_display_type = "PLAIN_AXES"
                obj.empty_display_size = self.axes_size
        if not join:
            for child_name, child_entities, matrix, material in children:
                child_transform = residual @ matrix
                child_parent = obj
                if not self.import_hierarchy:
                    child_transform = object_transform @ child_transform
                    child_parent = None
                yield from self.component(child_name, child_entities, child_transform,
                               material or inherited, child_parent, depth + 1)
        if self.create_bounds and obj is not None and (
            (obj.type == "EMPTY" and depth < self.hierarchy_levels)
            or (parent is None and len(self.objects) > start + 1)
        ):
            obj = self.add_assembly_bounds(obj, self.objects[start:])
        if self.progress:
            self.progress.component_done()
        yield
        return obj

    def add_assembly_bounds(self, root, objects):
        world_matrices = {}
        low = Vector((float("inf"),) * 3)
        high = Vector((float("-inf"),) * 3)
        def assembly_matrix(obj):
            if obj not in world_matrices:
                matrix = obj.matrix_parent_inverse @ obj.matrix_basis
                if obj != root:
                    matrix = assembly_matrix(obj.parent) @ matrix
                world_matrices[obj] = matrix
            return world_matrices[obj]

        for obj in objects:
            matrix = assembly_matrix(obj)
            if obj.data is None:
                continue
            for vertex in obj.data.vertices:
                point = matrix @ vertex.co
                for axis in range(3):
                    low[axis] = min(low[axis], point[axis])
                    high[axis] = max(high[axis], point[axis])
        if low.x == float("inf"):
            low = high = assembly_matrix(root).translation

        center = (low + high) * 0.5
        half_size = [max(extent * 0.505, 0.0005) for extent in high - low]
        geometry = Geometry()
        geometry.vertices = [
            tuple(half_size[axis] if index & (1 << axis) else -half_size[axis] for axis in range(3))
            for index in range(8)
        ]
        geometry.edges = {
            (index, index | (1 << axis))
            for index in range(8) for axis in range(3) if not index & (1 << axis)
        }
        name = f"{root.name} Bounds"
        mesh = self.mesh_from_geometry(name, geometry)
        box = self.create_object(name, mesh, root.parent, Matrix.Translation(center))
        box_inverse = Matrix.Translation(-center)
        if root.type == "EMPTY":
            for child in objects[1:]:
                if child.parent == root:
                    parent_inverse = box_inverse @ assembly_matrix(root) @ child.matrix_parent_inverse
                    child.parent = box
                    child.matrix_parent_inverse = parent_inverse
            self.objects.remove(root)
            bpy.data.objects.remove(root, do_unlink=True)
        else:
            root.parent = box
            root.matrix_parent_inverse = box_inverse @ root.matrix_parent_inverse
        return box

    def run(self, entities, model_name="Model Geometry"):
        if self.progress and self.progress.visible:
            self.progress.show("Counting SketchUp components…")
            yield
            counts = self.component_counts

            def count_components(parent):
                if parent.ptr not in counts:
                    counts[parent.ptr] = sum(1 + count_components(child)
                                             for _, child, _, _ in self.children(parent))
                return counts[parent.ptr]

            self.progress.total = count_components(entities)
            self.progress.show(f"0 / {self.progress.total:,} components imported")
            yield
        geometry = Geometry()
        if self.import_joined_mesh:
            yield from self.collect(geometry, entities, IDENTITY, None, self.children(entities))
            mesh = self.mesh_from_geometry(model_name, geometry)
            self.create_object(model_name, mesh, None, IDENTITY)
            return
        self.append_entities(geometry, entities, IDENTITY, None)
        if geometry.faces or geometry.edges:
            mesh = self.mesh_from_geometry("Model Geometry", geometry)
            self.create_object("Model Geometry", mesh, None, IDENTITY)
        for name, child_entities, matrix, material in self.children(entities):
            start = len(self.objects)
            root = yield from self.component(name, child_entities, matrix, material, None)
            if self.import_hierarchy:
                self.flatten_assembly(root, self.objects[start:])

    def flatten_assembly(self, root, objects):
        # Capture transforms before changing any parent links.
        relative = {root: IDENTITY}

        def relative_matrix(obj):
            if obj not in relative:
                relative[obj] = relative_matrix(obj.parent) @ obj.matrix_parent_inverse @ obj.matrix_basis
            return relative[obj]

        depths = {root: 0}

        def object_depth(obj):
            if obj not in depths:
                depths[obj] = object_depth(obj.parent) + 1
            return depths[obj]

        intermediate_empties = {
            obj for obj in objects if obj.type == "EMPTY" and object_depth(obj) >= self.hierarchy_levels
        }
        changes = []
        for obj in objects:
            if obj in intermediate_empties or object_depth(obj) <= self.hierarchy_levels:
                continue
            parent = obj.parent
            while object_depth(parent) >= self.hierarchy_levels:
                parent = parent.parent
            parent_inverse = relative_matrix(parent).inverted_safe() @ relative_matrix(obj.parent)
            changes.append((obj, parent, parent_inverse @ obj.matrix_parent_inverse))
        for obj, parent, parent_inverse in changes:
            obj.parent = parent
            obj.matrix_parent_inverse = parent_inverse
        for obj in intermediate_empties:
            self.objects.remove(obj)
            bpy.data.objects.remove(obj, do_unlink=True)

    def rollback(self):
        for datablocks, items in ((bpy.data.objects, self.objects), (bpy.data.meshes, self.meshes),
                                  (bpy.data.materials, self.new_materials), (bpy.data.images, self.images)):
            for item in items:
                datablocks.remove(item, do_unlink=True)
        if self.glass_group is not None and self.glass_group.users == 0:
            bpy.data.node_groups.remove(self.glass_group)


class ImportProgress:
    active = None

    def __init__(self, context, enabled=True):
        self.window = context.window
        self.workspace = context.workspace
        self.visible = enabled and not bpy.app.background and self.window is not None and self.workspace is not None
        self.components = 0
        self.total = None
        self.cancelled = False
        self.kept_partial_import = False
        self.next_update = 0.0

    def begin(self):
        ImportProgress.active = self
        if self.visible:
            self.draw_functions = bpy.types.STATUSBAR_HT_header._dyn_ui_initialize()
            self.original_draw = self.draw_functions[0]
            self.footer_draw = self.draw
            self.draw_functions[0] = self.footer_draw
            self.show("Opening SketchUp file…")

    def draw(self, header, context):
        if context.window != self.window:
            self.original_draw(header, context)
            return
        layout = header.layout
        layout.template_input_status()
        layout.separator_spacer()
        layout.template_reports_banner()
        layout.template_running_jobs()

        row = layout.row(align=True)
        row.label(text="Importing")
        bar = row.row(align=True)
        bar.ui_units_x = 6
        factor = self.components / max(self.total or 0, 1)
        bar.progress(factor=min(factor, 1.0), type="BAR", text=f"{factor:.0%}")
        row.operator("import_scene.skp_cancel", text="", icon="X")

        layout.separator_spacer()
        layout.row(align=True).template_status_info()

    def show(self, label):
        if self.visible:
            self.workspace.status_text_set(label)
            self.next_update = monotonic() + 0.25

    def component_done(self, count=1):
        self.components += count
        if self.visible and self.total is not None and monotonic() >= self.next_update:
            self.show(f"{self.components:,} / {self.total:,} components imported")

    def end(self):
        if self.visible and hasattr(self, "footer_draw"):
            if self.footer_draw in self.draw_functions:
                self.draw_functions[self.draw_functions.index(self.footer_draw)] = self.original_draw
            self.workspace.status_text_set(None)
        if ImportProgress.active is self:
            ImportProgress.active = None


class CancelSKPImport(bpy.types.Operator):
    bl_idname = "import_scene.skp_cancel"
    bl_label = "Cancel SketchUp Import"
    bl_description = "Stop the import and keep objects already imported. Use Undo to remove them"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        progress = ImportProgress.active
        return progress is not None and progress.window == context.window

    def execute(self, context):
        ImportProgress.active.cancelled = True
        return {"FINISHED"}


class ImportSKP(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.skp"
    bl_label = "Import SketchUp"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".skp"
    filter_glob: StringProperty(default="*.skp", options={"HIDDEN"})
    import_joined_mesh: BoolProperty(
        name="Single Mesh",
        description="Combine the entire SketchUp model into one mesh object",
        default=False,
    )
    hierarchy_levels: IntProperty(
        name="Hierarchy Levels",
        description="Maximum nesting depth. 0 imports unparented meshes. 1 makes parts direct children",
        default=1,
        min=0,
    )
    create_bounds: BoolProperty(
        name="Create Bounding Boxes",
        description="Create selectable bounding boxes for assemblies and geometry-free components",
        default=True,
    )
    auto_join: BoolProperty(
        name="Auto Join",
        description="Combine a component and its descendants when its child count reaches the threshold",
        default=True,
    )
    threshold: IntProperty(
        name="Join Threshold",
        description="Minimum child count for joining. Flat hierarchies count mesh parts at every nested level",
        default=5,
        min=1,
    )
    import_materials: BoolProperty(
        name="Materials",
        description="Create and assign materials from the SketchUp model",
        default=True,
    )
    import_textures: BoolProperty(
        name="Textures",
        description="Import image textures and pack them into the Blender file",
        default=True,
    )

    axes_size: FloatProperty(
        name="Empty Axe Size",
        description="Set the display size of the plain axes on imported Empty objects",
        default=0.2,
        min=0.0001,
        unit="LENGTH",
        precision=3,
    )

    show_progress: BoolProperty(
        name="Show Progress",
        description="Show import progress and allow cancellation. Counting components and updating the display may slow down the import",
        default=True,
    )

    def draw(self, context):
        self.layout.prop(self, "import_joined_mesh")
        options = self.layout.column()
        options.enabled = not self.import_joined_mesh
        options.prop(self, "hierarchy_levels")
        row = options.row()
        row.enabled = self.hierarchy_levels > 0
        row.prop(self, "auto_join")
        row = options.row()
        row.enabled = self.hierarchy_levels > 0 and self.auto_join
        row.prop(self, "threshold")
        row = options.row()
        row.enabled = self.hierarchy_levels > 0
        row.prop(self, "create_bounds")
        row = options.row()
        row.enabled = self.hierarchy_levels > 0 and not self.create_bounds
        row.prop(self, "axes_size")
        self.layout.prop(self, "import_materials")
        row = self.layout.row()
        row.enabled = self.import_materials
        row.prop(self, "import_textures")
        self.layout.prop(self, "show_progress")

    def execute(self, context):
        if ImportProgress.active is not None:
            self.report({"WARNING"}, "A SketchUp import is already running.")
            return {"CANCELLED"}
        self.progress = ImportProgress(context, enabled=self.show_progress)
        self.steps = self.import_steps(context.collection, self.progress)
        self.timer = None
        try:
            self.progress.begin()
            if self.progress.visible:
                self.timer = context.window_manager.event_timer_add(0.02, window=context.window)
                context.window_manager.modal_handler_add(self)
                return {"RUNNING_MODAL"}
            for _ in self.steps:
                pass
        except Exception as error:
            self.report({"ERROR"}, str(error))
            self.cancel(context)
            return {"CANCELLED"}
        self.cancel(context)
        return {"FINISHED"}

    def modal(self, context, event):
        if self.progress.cancelled or (event.type == "ESC" and event.value == "PRESS"):
            self.progress.cancelled = True
            self.cancel(context)
            if self.progress.kept_partial_import:
                self.report({"INFO"}, "SketchUp import stopped. Imported objects were kept.")
                return {"FINISHED"}
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        deadline = monotonic() + 0.02
        try:
            while True:
                next(self.steps)
                if monotonic() >= deadline:
                    return {"RUNNING_MODAL"}
        except StopIteration:
            self.cancel(context)
            return {"FINISHED"}
        except Exception as error:
            self.report({"ERROR"}, str(error))
            self.cancel(context)
            return {"CANCELLED"}

    def cancel(self, context):
        try:
            self.steps.close()
        finally:
            try:
                if self.timer is not None:
                    context.window_manager.event_timer_remove(self.timer)
                    self.timer = None
            finally:
                self.progress.end()

    def import_steps(self, parent_collection, progress):
        addon_dir = Path(__file__).resolve().parent
        if sys.platform == "darwin":
            bundled = addon_dir / "SketchUpAPI.framework" / "Versions" / "A" / "SketchUpAPI"
        else:
            bundled = addon_dir / "x64" / "SketchUpAPI.dll"
        sdk = SDK(bundled)
        sdk.invoke(sdk.dll.SUInitialize)
        try:
            model, status = Ref(), C.c_int()
            sdk.call("SUModelCreateFromFileWithStatus", C.pointer(model),
                     C.c_char_p(self.filepath.encode("utf-8")), C.pointer(status))
            try:
                if status.value:
                    self.report({"WARNING"}, "This file is newer than the installed SketchUp SDK. Some data may be missing.")
                collection = bpy.data.collections.new(Path(self.filepath).stem)
                parent_collection.children.link(collection)
                with TemporaryDirectory(prefix="blender-skp-") as directory:
                    importer = Importer(
                        sdk, collection, directory,
                        create_bounds=self.create_bounds, auto_join=self.auto_join,
                        threshold=self.threshold, import_materials=self.import_materials,
                        import_textures=self.import_textures, import_joined_mesh=self.import_joined_mesh,
                        hierarchy_levels=self.hierarchy_levels, axes_size=self.axes_size,
                        progress=progress,
                    )
                    try:
                        yield from importer.run(sdk.value("SUModelGetEntities", model), collection.name)
                    except BaseException as error:
                        if isinstance(error, GeneratorExit) and progress.cancelled and importer.objects:
                            progress.kept_partial_import = True
                        else:
                            importer.rollback()
                            bpy.data.collections.remove(collection)
                        raise
            finally:
                sdk.call("SUModelRelease", C.pointer(model))
        finally:
            sdk.invoke(sdk.dll.SUTerminate)


def menu_import(self, context):
    self.layout.operator(ImportSKP.bl_idname, text="SketchUp (.skp)")


def register():
    bpy.utils.register_class(CancelSKPImport)
    bpy.utils.register_class(ImportSKP)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    bpy.utils.unregister_class(ImportSKP)
    bpy.utils.unregister_class(CancelSKPImport)
