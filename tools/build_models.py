"""Rebuild the two model files `clean --scratches` downloads (see openfilmkit/models.py).

Maintainers only: needs torch, onnx and onnxruntime, which OpenFilmKit itself does not use.

  python tools/build_models.py OUTDIR BOPBTL_REPO FT_EPOCH_LATEST.pt MIGAN_PIPELINE_V2.onnx

- BOPBTL_REPO: a checkout of github.com/microsoft/Bringing-Old-Photos-Back-to-Life (for
  Global/detection_models; add an empty Global/detection_models/sync_batchnorm/__init__.py with
  `SynchronizedBatchNorm2d = None` if its submodule is missing: CPU inference does not use it).
- FT_EPOCH_LATEST.pt: checkpoints/detection/FT_Epoch_latest.pt from that repo's v1.0 release,
  global_checkpoints.zip (SHA-256 b2d7ab04e9b3885c6b1991bb7a0b823129dd6e3ac078a9fd059ebd2a7ba59a95).
- MIGAN_PIPELINE_V2.onnx: from huggingface.co/andraniksargsyan/migan (SHA-256 begins 6f1f3530a1a2324b).

Writes OUTDIR/scratch_detection_512_fp16.onnx (the UNet for one 512 px grayscale tile, sigmoid
included, weights stored as float16 and cast back to float32 on load) and
OUTDIR/migan_generator_512.onnx (the network cut out of the authors' pipeline between its input
concatenation and its RGB output, fixed at 1x4x512x512, constants folded so OpenCV can run it),
then checks both in OpenCV against the originals.
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import onnx
import onnxruntime as ort
import torch
from onnx import TensorProto, helper, numpy_helper

GEN_IN, GEN_OUT = '/Concat_23_output_0', '/model/synthesis/b512/Add_1_output_0'


def scratch_model(repo, checkpoint, out):
    sys.path.insert(0, str(Path(repo) / 'Global'))
    from detection_models import networks
    unet = networks.UNet(in_channels=1, out_channels=1, depth=4, conv_num=2, wf=6, padding=True, batch_norm=True,
                         up_mode='upsample', with_tanh=False, sync_bn=False, antialiasing=True)
    unet.load_state_dict(torch.load(checkpoint, map_location='cpu', weights_only=True)['model_state'])
    unet.eval()

    class Probability(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.unet = unet

        def forward(self, x):
            return torch.sigmoid(self.unet(x))

    net = Probability().eval()
    full = out.with_name('scratch_detection_512.onnx')
    torch.onnx.export(net, torch.zeros(1, 1, 512, 512), full, input_names=['gray'], output_names=['scratch'],
                      opset_version=17, dynamo=False, do_constant_folding=True)
    m = onnx.load(full)
    inits, casts = [], []
    for init in m.graph.initializer:
        a = numpy_helper.to_array(init)
        if a.dtype == np.float32 and a.size > 16:
            inits.append(numpy_helper.from_array(a.astype(np.float16), init.name + '_fp16'))
            casts.append(helper.make_node('Cast', [init.name + '_fp16'], [init.name], to=TensorProto.FLOAT))
        else:
            inits.append(init)
    m.graph.ClearField('initializer')
    m.graph.initializer.extend(inits)
    nodes = list(m.graph.node)
    m.graph.ClearField('node')
    m.graph.node.extend(casts + nodes)
    onnx.checker.check_model(m)
    onnx.save(m, out)
    full.unlink()
    x = np.random.default_rng(0).uniform(-1, 1, (1, 1, 512, 512)).astype(np.float32)
    with torch.no_grad():
        ref = net(torch.from_numpy(x)).numpy()
    d = cv2.dnn.readNetFromONNX(str(out))
    d.setInput(x)
    print(f'{out.name}: OpenCV vs PyTorch max difference {np.abs(d.forward() - ref).max():.4f}')


def fill_model(pipeline, out):
    raw = out.with_name('migan_generator_raw.onnx')
    onnx.utils.extract_model(str(pipeline), str(raw), [GEN_IN], [GEN_OUT])
    m = onnx.load(raw)
    names = {GEN_IN: 'input', GEN_OUT: 'output'}
    for t, shape in ((m.graph.input[0], [1, 4, 512, 512]), (m.graph.output[0], [1, 3, 512, 512])):
        t.type.tensor_type.ClearField('shape')
        for d in shape:
            t.type.tensor_type.shape.dim.add().dim_value = d
        t.name = names[t.name]
    for n in m.graph.node:
        n.input[:] = [names.get(i, i) for i in n.input]
        n.output[:] = [names.get(o, o) for o in n.output]
    m = onnx.shape_inference.infer_shapes(m)
    onnx.checker.check_model(m)
    onnx.save(m, raw)
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC   # portable folding only
    so.optimized_model_filepath = str(out)
    ref_sess = ort.InferenceSession(str(raw), so, providers=['CPUExecutionProvider'])
    x = np.random.default_rng(0).uniform(-1, 1, (1, 4, 512, 512)).astype(np.float32)
    ref = ref_sess.run(None, {'input': x})[0]
    raw.unlink()
    d = cv2.dnn.readNetFromONNX(str(out))
    d.setInput(x)
    print(f'{out.name}: OpenCV vs onnxruntime max difference {np.abs(d.forward() - ref).max():.2e}')


def main():
    outdir, repo, checkpoint, pipeline = sys.argv[1:]
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    scratch_model(repo, checkpoint, outdir / 'scratch_detection_512_fp16.onnx')
    fill_model(pipeline, outdir / 'migan_generator_512.onnx')


if __name__ == '__main__':
    main()
