import trimesh
import skimage
import skimage.measure
import numpy as np
from tqdm import tqdm
import torch
from lib.config import cfg
import pyrender
import os
import open3d as o3d
import lib.train.trainers.utils_colour as utils_colour

os.environ['PYOPENGL_PLATFORM'] = 'egl'


class Renderer():
    def __init__(self, height=480, width=640):
        self.renderer = pyrender.OffscreenRenderer(width, height)
        self.scene = pyrender.Scene()
        # self.render_flags = pyrender.RenderFlags.SKIP_CULL_FACES

    def __call__(self, height, width, intrinsics, pose, mesh):
        self.renderer.viewport_height = height
        self.renderer.viewport_width = width
        self.scene.clear()
        self.scene.add(mesh)
        cam = pyrender.IntrinsicsCamera(cx=intrinsics[0, 2], cy=intrinsics[1, 2],
                                        fx=intrinsics[0, 0], fy=intrinsics[1, 1])
        self.scene.add(cam, pose=self.fix_pose(pose))
        return self.renderer.render(self.scene)  # , self.render_flags)

    def fix_pose(self, pose):
        # 3D Rotation about the x-axis.
        t = np.pi
        c = np.cos(t)
        s = np.sin(t)
        R = np.array([[1, 0, 0],
                      [0, c, -s],
                      [0, s, c]])
        axis_transform = np.eye(4)
        axis_transform[:3, :3] = R
        return pose @ axis_transform

    def mesh_opengl(self, mesh):
        return pyrender.Mesh.from_trimesh(mesh)

    def delete(self):
        self.renderer.delete()


def refuse(mesh, data_loader,refuse_GT=False,scale=None,offset=None):
    #将颜色和几何点云混合？
    renderer = Renderer()
    mesh_opengl = renderer.mesh_opengl(mesh)
    if refuse_GT:
        volume = o3d.integration.ScalableTSDFVolume(
            voxel_length=0.04,
            sdf_trunc=3 * 0.04,
            color_type=o3d.integration.TSDFVolumeColorType.RGB8
        )
    else:
        # * cfg.test_dataset.scale
        volume = o3d.integration.ScalableTSDFVolume(
            voxel_length=0.01,
            sdf_trunc=3 * 0.01,
            color_type=o3d.integration.TSDFVolumeColorType.RGB8
        )
    for batch in tqdm(data_loader, desc='Refusing'):
        for b in range(batch['rgb'].shape[0]):
            h, w = batch['meta']['h'].item(), batch['meta']['w'].item()

            intrinsic = np.eye(4)
            intrinsic[:3, :3] = batch['intrinsic'][b].numpy()
            pose = batch['c2w'][b].numpy()
            if refuse_GT:
                pose[:3, 3]=pose[:3, 3]/scale+offset #需要变换pose
            rgb = batch['rgb'][b].view(h, w, 3).numpy()
            rgb = (rgb * 255).astype(np.uint8)
            rgb = o3d.geometry.Image(rgb)
            _, depth_pred = renderer(h, w, intrinsic, pose, mesh_opengl)
            depth_pred = o3d.geometry.Image(depth_pred)
            rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                rgb, depth_pred, depth_scale=1.0, depth_trunc=5.0, convert_rgb_to_intensity=False
            )
            fx, fy, cx, cy = intrinsic[0, 0], intrinsic[1, 1], intrinsic[0, 2], intrinsic[1, 2]
            intrinsic = o3d.camera.PinholeCameraIntrinsic(width=w, height=h, fx=fx,  fy=fy, cx=cx, cy=cy)
            extrinsic = np.linalg.inv(pose)
            volume.integrate(rgbd, intrinsic, extrinsic)
    
    return volume.extract_triangle_mesh()


def transform(mesh, scale, offset):
    v = np.asarray(mesh.vertices)
    v /= scale
    v += offset
    mesh.vertices = o3d.utility.Vector3dVector(v)
    return mesh


def extract_mesh(sdf_net, 
                 level=0.0, 
                 N=512, 
                 chunk=100000,
                 seg_mesh = False,
                 semantic_net = None):
    s = cfg.model.bounding_radius * 2
    voxel_grid_origin = [-s/2., -s/2., -s/2.]
    volume_size = [s, s, s]

    overall_index = np.arange(0, N ** 3, 1).astype(int)
    xyz = np.zeros([N ** 3, 3])

    xyz[:, 2] = overall_index % N
    xyz[:, 1] = (overall_index // N) % N
    xyz[:, 0] = ((overall_index // N) // N) % N

    xyz = xyz * (s / (N - 1)) + voxel_grid_origin
    
    def batchify(query_fn, inputs: torch.Tensor, chunk=chunk):
        sdf = []
        for i in tqdm(range(0, inputs.shape[0], chunk), desc='Querying SDF'):
            sdf_i = query_fn(torch.from_numpy(inputs[i:i+chunk]).float().cuda()).data.cpu().numpy()
            sdf.append(sdf_i)
        sdf = np.concatenate(sdf, axis=0)
        return sdf

    sdf = batchify(sdf_net.forward, xyz)
    sdf = sdf.reshape([N, N, N])

    vertices, faces, normals, values = skimage.measure.marching_cubes(
        sdf, level=level, spacing=[float(v) / N for v in volume_size]
    )

    vertices += voxel_grid_origin
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces)

    surface_label = []
    semmesh = mesh
    if seg_mesh:
        vertices_tensor = torch.FloatTensor(vertices.copy()).reshape([-1, 3]).cuda()
        for i in tqdm(range(0, vertices_tensor.shape[0], chunk), desc='Querying Surface Label'):
            sdf_i, nablas_i, geometry_feature_i = sdf_net.forward_with_nablas(vertices_tensor[i:i+chunk])
            semantics = semantic_net.forward(vertices_tensor[i:i+chunk], geometry_feature_i)
            surface_label.append(semantics.argmax(axis=1).cpu().numpy())
        surface_label = np.concatenate(surface_label, axis=0)
        
        semantic_class=cfg.model.semantic.semantic_class
        if semantic_class==3:
            colour_map_np = utils_colour.nyu3_colour_code
        elif semantic_class==40 or semantic_class==41:
            colour_map_np = utils_colour.nyu40_colour_code
            surface_label = surface_label + 1
        
        surface_labels_vis = colour_map_np[(surface_label)].astype(np.uint8)
        semmesh = trimesh.Trimesh(vertices=vertices, 
                                faces=faces, 
                                vertex_colors = surface_labels_vis,
                                process=False)
        
    return mesh, semmesh, surface_label
