'use strict';

(function exposeMonitorChart(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AxisMonitorChart = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  const DEFAULT_WINDOW_MS = 15 * 60 * 1000;
  const RANGE_AXIS_LABELS = Object.freeze({
    15: Object.freeze(['-15m', '-10m', '-5m', '现在']),
    60: Object.freeze(['-1h', '-40m', '-20m', '现在']),
    1440: Object.freeze(['-24h', '-16h', '-8h', '现在']),
  });
  const WIDTH = 900;
  const HEIGHT = 200;

  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function rounded(value) { return finite(value) ? Math.round(value) : '--'; }
  function supportedWindowMinutes(value) {
    const minutes = Number(value);
    if (!Number.isInteger(minutes) || !RANGE_AXIS_LABELS[minutes]) throw new RangeError(`unsupported history window: ${value}`);
    return minutes;
  }
  function axisLabels(minutes) { return [...RANGE_AXIS_LABELS[supportedWindowMinutes(minutes)]]; }
  function windowMilliseconds(minutes) { return supportedWindowMinutes(minutes) * 60 * 1000; }

  function buildChartModel(samples, getter, endTimeMs = Date.now(), windowMs = DEFAULT_WINDOW_MS, options = {}) {
    if (typeof getter !== 'function') throw new TypeError('getter must be a function');
    if (!finite(endTimeMs) || !finite(windowMs) || windowMs <= 0) throw new RangeError('chart time window is invalid');
    const minimum = options.minimum ?? 0; const maximum = options.maximum ?? 100;
    if (!finite(minimum) || !finite(maximum) || maximum <= minimum) throw new RangeError('chart value range is invalid');
    const startTimeMs = endTimeMs - windowMs;
    const entries = (Array.isArray(samples) ? samples : []).map((sample) => ({
      timestamp: Date.parse(sample?.sampled_at),
      value: getter(sample),
    })).filter((entry) => Number.isFinite(entry.timestamp) && entry.timestamp >= startTimeMs && entry.timestamp <= endTimeMs).sort((left, right) => left.timestamp - right.timestamp);

    const segments = []; let segment = [];
    entries.forEach((entry) => {
      if (!finite(entry.value)) { if (segment.length) segments.push(segment); segment = []; return; }
      const plotted = Math.min(maximum, Math.max(minimum, entry.value));
      segment.push({ x: ((entry.timestamp - startTimeMs) / windowMs) * WIDTH, y: HEIGHT - ((plotted - minimum) / (maximum - minimum)) * HEIGHT, value: entry.value, timestamp: entry.timestamp });
    });
    if (segment.length) segments.push(segment);

    const points = segments.flat(); const values = points.map((point) => point.value); const latest = entries.at(-1);
    return {
      current: latest ? rounded(latest.value) : '--',
      average: values.length ? Math.round(values.reduce((sum, value) => sum + value, 0) / values.length) : '--',
      peak: values.length ? Math.round(Math.max(...values)) : '--',
      minimum: values.length ? Math.round(Math.min(...values)) : '--',
      segments,
      pointCount: points.length,
      lastPoint: points.at(-1) || null,
      minimumScale: minimum,
      maximumScale: maximum,
      startTimeMs,
      endTimeMs,
    };
  }

  function buildChartGeometry(model) {
    const segments = Array.isArray(model?.segments) ? model.segments : [];
    return {
      lines: segments.filter((segment) => segment.length > 1),
      areas: segments.filter((segment) => segment.length > 1),
      isolatedPoints: segments.filter((segment) => segment.length === 1).map((segment) => segment[0]),
    };
  }

  function nearestPoint(model, x) {
    if (!finite(x)) return null;
    const points = (Array.isArray(model?.segments) ? model.segments : []).flat();
    return points.reduce((nearest, point) => nearest === null || Math.abs(point.x - x) < Math.abs(nearest.x - x) ? point : nearest, null);
  }

  function nearestSample(samples, targetTimestamp, startTimeMs = -Infinity, endTimeMs = Infinity) {
    if (!finite(targetTimestamp)) return null;
    return (Array.isArray(samples) ? samples : []).map((sample) => ({ sample, timestamp: Date.parse(sample?.sampled_at) }))
      .filter((entry) => Number.isFinite(entry.timestamp) && entry.timestamp >= startTimeMs && entry.timestamp <= endTimeMs)
      .reduce((nearest, entry) => nearest === null || Math.abs(entry.timestamp - targetTimestamp) < Math.abs(nearest.timestamp - targetTimestamp) ? entry : nearest, null)?.sample || null;
  }

  return { axisLabels, windowMilliseconds, buildChartModel, buildChartGeometry, nearestPoint, nearestSample };
}));
