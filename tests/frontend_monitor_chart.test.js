'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const monitorChart = require('../monitor-chart.js');

test('touch gesture selects only horizontal movement and keeps the final selection', () => {
  const started = monitorChart.beginPointerGesture(7, 100, 200);
  const vertical = monitorChart.movePointerGesture(started, 7, 102, 220);
  assert.equal(vertical.select, false);
  assert.equal(vertical.gesture.dragging, false);

  const horizontal = monitorChart.movePointerGesture(vertical.gesture, 7, 120, 202);
  assert.equal(horizontal.select, true);
  assert.equal(horizontal.gesture.dragging, true);

  const released = monitorChart.finishPointerGesture(horizontal.gesture, 7, false);
  assert.deepEqual(released, { gesture: null, select: true, finished: true });
});

test('cancelled and unrelated touch pointers do not replace the last selection', () => {
  const started = monitorChart.beginPointerGesture(3, 10, 10);
  const unrelated = monitorChart.movePointerGesture(started, 4, 40, 10);
  assert.equal(unrelated.select, false);
  assert.equal(unrelated.gesture, started);

  const moved = monitorChart.movePointerGesture(started, 3, 30, 10);
  const cancelled = monitorChart.finishPointerGesture(moved.gesture, 3, true);
  assert.deepEqual(cancelled, { gesture: null, select: false, finished: true });
});

const minute = 60 * 1000;
const end = Date.parse('2026-08-28T12:15:00Z');
const sample = (minuteOffset, value) => ({ sampled_at: new Date(end - minuteOffset * minute).toISOString(), value });

test('chart model positions samples on the fixed fifteen-minute timeline', () => {
  const model = monitorChart.buildChartModel([sample(14, 10), sample(5, null), sample(1, 30)], (item) => item.value, end);

  assert.deepEqual(model.segments.map((segment) => segment.map((point) => point.x)), [[60], [840]]);
  assert.equal(model.current, 30);
  assert.equal(model.average, 20);
  assert.equal(model.peak, 30);
  assert.equal(model.minimum, 10);
});

test('latest missing value does not reuse an older sample as current', () => {
  const model = monitorChart.buildChartModel([sample(1, 30), sample(0, null)], (item) => item.value, end);

  assert.equal(model.current, '--');
  assert.equal(model.average, 30);
  assert.equal(model.lastPoint.x, 840);
});

test('single and out-of-window samples preserve their real position', () => {
  const model = monitorChart.buildChartModel([sample(20, 99), sample(2, 40)], (item) => item.value, end);

  assert.equal(model.pointCount, 1);
  assert.equal(model.segments[0][0].x, 780);
  assert.equal(model.segments[0][0].y, 120);
});

test('isolated valid samples remain visible across missing intervals', () => {
  const model = monitorChart.buildChartModel([sample(5, 10), sample(4, null), sample(3, 20), sample(2, null), sample(1, 30)], (item) => item.value, end);
  const geometry = monitorChart.buildChartGeometry(model);

  assert.equal(geometry.lines.length, 0);
  assert.equal(geometry.areas.length, 0);
  assert.deepEqual(geometry.isolatedPoints.map((point) => point.value), [10, 20, 30]);
});

test('time ranges expose matching axis labels and durations', () => {
  assert.deepEqual(monitorChart.axisLabels(15), ['-15m', '-10m', '-5m', '现在']);
  assert.deepEqual(monitorChart.axisLabels(60), ['-1h', '-40m', '-20m', '现在']);
  assert.deepEqual(monitorChart.axisLabels(1440), ['-24h', '-16h', '-8h', '现在']);
  assert.equal(monitorChart.windowMilliseconds(60), 60 * 60 * 1000);
  assert.throws(() => monitorChart.axisLabels(30), /unsupported/i);
});

test('chart model scales non-percent GPU telemetry without losing raw values', () => {
  const model = monitorChart.buildChartModel(
    [sample(2, 1000), sample(1, 2500)], (item) => item.value, end, 15 * minute,
    { minimum: 0, maximum: 3000 },
  );

  assert.equal(model.current, 2500);
  assert.equal(model.peak, 2500);
  assert.equal(Math.round(model.lastPoint.y), 33);
  assert.equal(monitorChart.nearestPoint(model, 830).value, 2500);
});

test('VRAM chart keeps one-decimal GiB statistics against physical capacity', () => {
  const model = monitorChart.buildChartModel(
    [sample(2, 43.25), sample(1, 44.75)], (item) => item.value, end, 15 * minute,
    { minimum: 0, maximum: 48, precision: 1 },
  );

  assert.equal(model.current, 44.8);
  assert.equal(model.average, 44);
  assert.equal(model.peak, 44.8);
  assert.equal(model.minimum, 43.3);
  assert.equal(model.maximumScale, 48);
});

test('correlation selection keeps one sample timestamp when a metric is missing', () => {
  const samples = [
    { ...sample(2, 80), clock: null },
    { ...sample(1, 90), clock: 2500 },
  ];
  const selected = monitorChart.nearestSample(samples, Date.parse(samples[0].sampled_at));

  assert.equal(selected.sampled_at, samples[0].sampled_at);
  assert.equal(selected.value, 80);
  assert.equal(selected.clock, null);
});
