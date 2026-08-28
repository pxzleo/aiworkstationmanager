'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const monitorChart = require('../monitor-chart.js');

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
