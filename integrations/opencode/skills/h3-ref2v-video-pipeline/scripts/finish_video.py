import argparse
import hashlib
import json
import subprocess
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser(description='Merge original source audio into the generated video, with optional verification')
parser.add_argument('--workdir', default='.', help='directory containing submission/history/report files')
parser.add_argument('--submission', default=None, help='submit json written by submit_wait.ps1 (omit when using --generated)')
parser.add_argument('--history', default=None, help='history json to write (name only; optional, single-segment only)')
parser.add_argument('--generated', default=None, help='explicit generated/concatenated mp4 to finish; when set, --submission/--history are ignored and no /history lookup is done (use for split / multi-segment results)')
parser.add_argument('--source', required=True, help='original source video with audio')
parser.add_argument('--output', required=True, help='final video to create (must not exist yet)')
parser.add_argument('--verify', action='store_true', help='verify output media and source-audio identity')
parser.add_argument('--report', default=None, help='verification report json (name only; also enables verification for compatibility)')
parser.add_argument('--trim', type=float, default=None, help='output video duration in seconds; default = source video stream duration')
parser.add_argument('--scale', default=None, help='WxH e.g. 720:1280; default = source video WxH')
parser.add_argument('--fps', type=int, default=None, help='output fps; default = source video fps')
parser.add_argument('--api', default='http://127.0.0.1:8189')
parser.add_argument('--output-root', default=None, help='ComfyUI output root; required with --submission')
args = parser.parse_args()
workdir = Path(args.workdir)

verify_requested = args.verify or bool(args.report)
if verify_requested and not args.report:
    raise ValueError('--report is required with --verify')

if args.generated:
    generated = Path(args.generated)
    if not generated.exists():
        raise FileNotFoundError(f'--generated video not found: {generated}')
else:
    if not args.submission:
        raise ValueError('Provide either --generated or --submission')
    if not args.output_root:
        raise ValueError('--output-root is required when using --submission')
    submission = json.loads((workdir / args.submission).read_text(encoding='utf-8-sig'))
    prompt_id = submission['prompt_id']
    with urllib.request.urlopen(f'{args.api}/history/{prompt_id}') as response:
        history = json.load(response)
    if prompt_id not in history:
        raise RuntimeError('Generation has not finished')
    run = history[prompt_id]
    if args.history:
        (workdir / args.history).write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
    if run['status']['status_str'] != 'success':
        raise RuntimeError(f"Generation failed: {run['status']}")
    asset = run['outputs']['92']['images'][0]
    generated = Path(args.output_root) / asset['subfolder'] / asset['filename']

source = Path(args.source)
destination = Path(args.output)
if destination.exists():
    raise FileExistsError(f'Will not overwrite existing output: {destination}')

probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
    'stream=codec_type,codec_name,width,height,r_frame_rate,duration,nb_frames', '-of', 'json', str(source)]))
vstream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
trim = args.trim if args.trim is not None else float(vstream['duration'])
num, _, den = vstream['r_frame_rate'].partition('/')
fps = args.fps if args.fps is not None else int(round(int(num) / (int(den) or 1)))
scale = args.scale if args.scale else f"{vstream['width']}:{vstream['height']}"

subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(generated), '-i', str(source),
                '-map', '0:v:0', '-map', '1:a:0',
                '-vf', f'trim=duration={trim:g},setpts=PTS-STARTPTS,scale={scale}:flags=lanczos,fps={fps}',
                '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy',
                '-movflags', '+faststart', str(destination)], check=True)

report = {'output': str(destination), 'generated': str(generated), 'trim': trim, 'scale': scale, 'fps': fps,
          'verified': False}

if not verify_requested:
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0)

def audio_hash(path):
    data = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0', '-c:a', 'copy', '-f', 'adts', 'pipe:1'])
    return hashlib.sha256(data).hexdigest()

source_hash = audio_hash(source)
output_hash = audio_hash(destination)
if source_hash != output_hash:
    raise RuntimeError('Output audio differs from original source audio')

out_probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
    'format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,duration,nb_frames', '-of', 'json', str(destination)]))
report.update(verified=True, original_audio_sha256=source_hash, output_audio_sha256=output_hash,
              audio_bitstream_identical=True, probe=out_probe)

def audio_packets(path):
    data = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'packet=pts_time,dts_time,duration_time', '-of', 'json', str(path)])
    return json.loads(data)['packets']

if audio_packets(source) != audio_packets(destination):
    raise RuntimeError('Output audio packet timing differs from original')

def pcm_hash(path):
    data = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
        '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1'])
    return hashlib.sha256(data).hexdigest()

original_pcm_hash = pcm_hash(source)
if original_pcm_hash != pcm_hash(destination):
    raise RuntimeError('Decoded output audio differs from original')

report.update(audio_packet_timestamps_identical=True, decoded_audio_identical=True,
              decoded_audio_sha256=original_pcm_hash)
(workdir / args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
