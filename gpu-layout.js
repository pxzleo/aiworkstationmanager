'use strict';

(function exposeGpuLayout(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AxisGpuLayout = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  function validGpuIndex(value) {
    if (typeof value === 'boolean' || value === null || value === '') return null;
    const number = Number(value);
    return Number.isInteger(number) && number >= 0 ? number : null;
  }

  function normalizedUuid(gpu) {
    return typeof gpu?.uuid === 'string' ? gpu.uuid.trim() : '';
  }

  function prepareGpus(gpus) {
    const ordered = (Array.isArray(gpus) ? gpus : []).map((gpu, sourcePosition) => ({ gpu, sourcePosition })).sort((left, right) => {
      const leftIndex = validGpuIndex(left.gpu?.index);
      const rightIndex = validGpuIndex(right.gpu?.index);
      if (leftIndex !== null && rightIndex !== null && leftIndex !== rightIndex) return leftIndex - rightIndex;
      if ((leftIndex !== null) !== (rightIndex !== null)) return leftIndex !== null ? -1 : 1;
      return left.sourcePosition - right.sourcePosition;
    });
    const occurrences = new Map();
    return ordered.map(({ gpu }) => {
      const uuid = normalizedUuid(gpu);
      const index = validGpuIndex(gpu?.index);
      const base = uuid ? `uuid:${uuid}` : index !== null ? `index:${index}` : 'unknown';
      const occurrence = occurrences.get(base) || 0;
      occurrences.set(base, occurrence + 1);
      return { ...gpu, _uiKey: `${base}#${occurrence}` };
    });
  }

  function gpuSetSignature(gpus) {
    return (Array.isArray(gpus) ? gpus : []).map((gpu) => `${gpu._uiKey || 'unprepared'}@${validGpuIndex(gpu.index) ?? '?'}`).join('|');
  }

  function metricForGpu(sample, current, field) {
    const sampledGpus = Array.isArray(sample?.gpus) ? sample.gpus : [];
    const uuid = normalizedUuid(current);
    let matches;
    if (uuid) {
      matches = sampledGpus.filter((gpu) => normalizedUuid(gpu) === uuid);
      if (matches.length > 1) {
        const index = validGpuIndex(current?.index);
        if (index !== null) matches = matches.filter((gpu) => validGpuIndex(gpu?.index) === index);
      }
    }
    else {
      const index = validGpuIndex(current?.index);
      if (index === null) return null;
      matches = sampledGpus.filter((gpu) => validGpuIndex(gpu?.index) === index);
    }
    return matches.length === 1 ? matches[0][field] ?? null : null;
  }

  function normalizeLabel(value) {
    return String(value || '').trim().toLowerCase().replace(/\s+/g, ' ');
  }

  function gpuIndicesFromLabel(label) {
    return [...label.matchAll(/(?:^|[^a-z0-9])gpu[\s_-]*0*(\d+)(?=$|[^a-z0-9])/gi)].map((match) => validGpuIndex(match[1])).filter((index) => index !== null);
  }

  function modelNumbers(value) {
    return [...normalizeLabel(value).matchAll(/(?:^|[^0-9])(\d{3,5})(?=$|[^0-9])/g)].map((match) => match[1]);
  }

  function serviceGpuKeys(service, gpus) {
    const label = normalizeLabel(service?.gpu_label);
    if (!label) return [];
    const candidates = Array.isArray(gpus) ? gpus : [];
    const matchedKeys = new Set();
    const labeledIndices = gpuIndicesFromLabel(label);
    if (labeledIndices.length) {
      labeledIndices.forEach((labeledIndex) => {
        const matches = candidates.filter((gpu) => validGpuIndex(gpu.index) === labeledIndex);
        if (matches.length === 1) matchedKeys.add(matches[0]._uiKey);
      });
      return candidates.filter((gpu) => matchedKeys.has(gpu._uiKey)).map((gpu) => gpu._uiKey);
    }
    const exactMatches = candidates.filter((gpu) => normalizeLabel(gpu.name) === label);
    if (exactMatches.length === 1) return [exactMatches[0]._uiKey];
    const numbers = modelNumbers(label);
    if (!numbers.length) return [];
    numbers.forEach((number) => {
      const matches = candidates.filter((gpu) => new Set(modelNumbers(gpu.name)).has(number));
      if (matches.length === 1) matchedKeys.add(matches[0]._uiKey);
    });
    return candidates.filter((gpu) => matchedKeys.has(gpu._uiKey)).map((gpu) => gpu._uiKey);
  }

  function serviceGpuKey(service, gpus) {
    const keys = serviceGpuKeys(service, gpus);
    return keys.length === 1 ? keys[0] : null;
  }

  return { validGpuIndex, prepareGpus, gpuSetSignature, metricForGpu, serviceGpuKey, serviceGpuKeys };
}));
