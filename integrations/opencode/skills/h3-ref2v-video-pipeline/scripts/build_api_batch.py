import argparse
import copy
import json
from pathlib import Path

parser = argparse.ArgumentParser(description='Derive a single H3 batch API graph: N segments as parallel branches in one prompt (one scene switch for the whole batch)')
parser.add_argument('--baseline', required=True, help='baseline API graph (4-step full_api.json or 8-step full_8step_api.json)')
parser.add_argument('--out', required=True, help='output batch API graph json')
parser.add_argument('--source', required=True, help='source video path')
parser.add_argument('--width', type=int, required=True)
parser.add_argument('--height', type=int, required=True)
parser.add_argument('--prefix', required=True, help='SaveVideo prefix base, e.g. task-name; per-segment prefix becomes <prefix>/<tag>')
parser.add_argument('--spec', required=True, help='segments spec json: [{"length":107,"skip":0,"prompt_file":"p01.txt","tag":"seg01"}, ...]')
parser.add_argument('--seed', type=int, default=None, help='same noise_seed for all segments (keep baseline seed if omitted); same seed keeps faces consistent across segments')
args = parser.parse_args()

graph = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
spec = json.loads(Path(args.spec).read_text(encoding='utf-8-sig'))
if not spec:
    raise SystemExit('spec is empty')

ROLE_IDS = {'150': 'vhs', '136': 'h3', '129': 'noise', '125': 'samp', '126': 'guider', '122': 'dec', '130': 'cv', '92': 'sv'}
PER_SEG_IDS = set(ROLE_IDS) & set(graph)
if not {'136', '129', '125', '126', '122', '130', '92'} <= set(graph):
    raise SystemExit('baseline is missing required H3 nodes (136/129/125/126/122/130/92)')

def clone_id(i, role):
    return {
        'vhs': 350 + i, 'h3': 360 + i, 'noise': 370 + i, 'guider': 380 + i,
        'samp': 390 + i, 'dec': 400 + i, 'cv': 410 + i, 'sv': 420 + i,
    }[role]

def remap_inputs(inputs, mapping):
    out = {}
    for k, v in inputs.items():
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in mapping:
            out[k] = [str(mapping[v[0]]), v[1]]
        else:
            out[k] = copy.deepcopy(v)
    return out

for i, seg in enumerate(spec):
    for key in ('length', 'skip', 'prompt_file', 'tag'):
        if key not in seg:
            raise SystemExit(f'segment {i} missing key: {key}')
    length = int(seg['length'])
    if length % 17 != 5:
        raise SystemExit(f'segment {i} length {length} not on the 17n+5 grid')
    tag = str(seg['tag'])
    prompt = Path(seg['prompt_file']).read_text(encoding='utf-8-sig').replace('\r\n', '\n').rstrip('\n')

    if i == 0:
        ids = {role: int(src) for src, role in ROLE_IDS.items() if src in graph}
        targets = {int(k): graph[k] for k in graph if int(k) in ids.values()}
        inplace = True
    else:
        src_by_role = {v: k for k, v in ROLE_IDS.items()}
        ids = {role: clone_id(i, role) for role in ROLE_IDS.values() if src_by_role[role] in graph}
        mapping = {src: str(ids[role]) for src, role in ROLE_IDS.items() if src in graph}
        targets = {}
        for src, role in ROLE_IDS.items():
            if src not in graph:
                continue
            node = copy.deepcopy(graph[src])
            node['inputs'] = remap_inputs(node['inputs'], mapping)
            node.setdefault('_meta', {})['title'] = f'{node["class_type"]} [seg{tag}]'
            targets[ids[role]] = node
        inplace = False

    if ids.get('vhs') is not None:
        v = targets[ids['vhs']]['inputs']
        v['video'] = args.source
        v['custom_width'] = args.width
        v['custom_height'] = args.height
        v['frame_load_cap'] = length
        v['skip_first_frames'] = int(seg['skip'])
    h = targets[ids['h3']]['inputs']
    h['prompt'] = prompt
    h['width'] = args.width
    h['height'] = args.height
    h['length'] = length
    if 'ref_videos.ref_video_0' in h:
        h['ref_videos.ref_video_0'] = [str(ids['vhs']), 0]
    targets[ids['noise']]['inputs']['noise_seed'] = args.seed if args.seed is not None else targets[ids['noise']]['inputs'].get('noise_seed')
    cv = targets[ids['cv']]['inputs']
    cv['images'] = [str(ids['dec']), 0]
    if 'audio' in cv:
        cv['audio'] = [str(ids['dec']), 1]
    sv = targets[ids['sv']]['inputs']
    sv['video'] = [str(ids['cv']), 0]
    sv['filename_prefix'] = f'{args.prefix}/{tag}'
    if not inplace:
        for nid, node in targets.items():
            graph[str(nid)] = node

total_frames = sum(int(s['length']) for s in spec)
out = Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'written {out}')
print(f'segments={len(spec)} total_frames={total_frames} (~{total_frames / 24:.2f}s @24fps) seed={args.seed or "baseline"} decode={graph["122"]["class_type"]}')
for i, seg in enumerate(spec):
    h3_id = 136 if i == 0 else clone_id(i, 'h3')
    print(f'  seg {i}: tag={seg["tag"]} length={seg["length"]} skip={seg["skip"]} h3_node={h3_id}')
