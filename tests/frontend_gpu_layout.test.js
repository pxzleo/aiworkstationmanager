'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const gpuLayout = require('../gpu-layout.js');

test('prepareGpus sorts valid indices and gives every card a unique stable key', () => {
  const prepared = gpuLayout.prepareGpus([
    { index: null, name: 'Unknown' },
    { index: 10, uuid: 'same', name: 'GPU 10' },
    { index: 1, uuid: 'same', name: 'GPU 1' },
    { index: 1, uuid: 'same', name: 'GPU 1 duplicate' },
    { index: null, name: 'Unknown duplicate' },
  ]);

  assert.deepEqual(prepared.map((gpu) => gpu.index), [1, 1, 10, null, null]);
  assert.equal(new Set(prepared.map((gpu) => gpu._uiKey)).size, prepared.length);
  assert.deepEqual(gpuLayout.prepareGpus([]), []);
});

test('metricForGpu prefers UUID and never treats a missing index as GPU 0', () => {
  const sample = {
    gpus: [
      { index: 0, uuid: 'old-card', load_percent: 12 },
      { index: 4, uuid: 'new-card', load_percent: 76 },
    ],
  };

  assert.equal(gpuLayout.metricForGpu(sample, { index: 0, uuid: 'new-card' }, 'load_percent'), 76);
  assert.equal(gpuLayout.metricForGpu(sample, { index: 0, uuid: 'missing-card' }, 'load_percent'), null);
  assert.equal(gpuLayout.metricForGpu(sample, { index: null }, 'load_percent'), null);
  assert.equal(gpuLayout.metricForGpu({ gpus: [{ index: 0, load_percent: 31 }] }, { index: 0 }, 'load_percent'), 31);
  assert.equal(gpuLayout.metricForGpu({ gpus: [{ index: 0 }, { index: 0 }] }, { index: 0 }, 'load_percent'), null);
  assert.equal(gpuLayout.metricForGpu({ gpus: [{ index: 0, uuid: 'duplicate', load_percent: 20 }, { index: 1, uuid: 'duplicate', load_percent: 80 }] }, { index: 1, uuid: 'duplicate' }, 'load_percent'), 80);
});

test('serviceGpuKey uses exact GPU indices and rejects ambiguous model labels', () => {
  const gpus = gpuLayout.prepareGpus([
    { index: 0, name: 'NVIDIA GeForce RTX 4090' },
    { index: 1, name: 'NVIDIA GeForce RTX 3090' },
    { index: 10, name: 'NVIDIA A100' },
  ]);

  assert.equal(gpuLayout.serviceGpuKey({ gpu_label: 'GPU 10' }, gpus), gpus[2]._uiKey);
  assert.equal(gpuLayout.serviceGpuKey({ gpu_label: 'GPU01' }, gpus), gpus[1]._uiKey);
  assert.equal(gpuLayout.serviceGpuKey({ gpu_label: 'RTX 4090' }, gpus), gpus[0]._uiKey);
  assert.equal(gpuLayout.serviceGpuKey({ gpu_label: 'RTX' }, gpus), null);

  const duplicateModels = gpuLayout.prepareGpus([
    { index: 0, name: 'NVIDIA GeForce RTX 4090' },
    { index: 1, name: 'NVIDIA GeForce RTX 4090' },
  ]);
  assert.equal(gpuLayout.serviceGpuKey({ gpu_label: '4090' }, duplicateModels), null);
});

test('serviceGpuKeys maps an explicitly multi-GPU service to every named card', () => {
  const gpus = gpuLayout.prepareGpus([
    { index: 0, name: 'NVIDIA GeForce RTX 4090' },
    { index: 1, name: 'NVIDIA GeForce RTX 3090' },
  ]);

  assert.deepEqual(gpuLayout.serviceGpuKeys({ gpu_label: 'RTX 4090 + RTX 3090' }, gpus), [gpus[0]._uiKey, gpus[1]._uiKey]);
  assert.deepEqual(gpuLayout.serviceGpuKeys({ gpu_label: 'GPU 0 + GPU 1' }, gpus), [gpus[0]._uiKey, gpus[1]._uiKey]);
  assert.deepEqual(gpuLayout.serviceGpuKeys({ gpu_label: 'RTX' }, gpus), []);
});

test('gpuSetSignature tracks card identity and order', () => {
  const first = gpuLayout.prepareGpus([{ index: 0, uuid: 'a' }, { index: 1, uuid: 'b' }]);
  const changed = gpuLayout.prepareGpus([{ index: 0, uuid: 'b' }, { index: 1, uuid: 'a' }]);
  assert.notEqual(gpuLayout.gpuSetSignature(first), gpuLayout.gpuSetSignature(changed));
});
