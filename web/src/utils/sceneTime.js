import { formatSourceTime, parseSourceTime } from "./sourceTime.js";

const DAY = /^\d{4}-\d{2}-\d{2}$/u;
const CLOCK = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/u;
const shanghaiDay = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
});

export function sceneTimeValue(scene) {
  return String(scene.memoryTime || scene.date || "").trim();
}

function sceneDay(scene) {
  if (DAY.test(scene.date || "")) return scene.date;
  const value = sceneTimeValue(scene);
  if (DAY.test(value)) return value;
  const parsed = CLOCK.test(value) ? parseSourceTime(value) : null;
  return parsed ? shanghaiDay.format(parsed) : "";
}

function recordedTime(scene) {
  const value = sceneTimeValue(scene);
  const precise = CLOCK.test(value) ? value : scene.recordedAt;
  return CLOCK.test(precise || "") ? parseSourceTime(precise)?.getTime() ?? -Infinity : -Infinity;
}

export function compareScenesByTime(left, right) {
  const dayOrder = sceneDay(right).localeCompare(sceneDay(left));
  if (dayOrder) return dayOrder;
  const leftTime = recordedTime(left);
  const rightTime = recordedTime(right);
  if (leftTime !== rightTime) return leftTime < rightTime ? 1 : -1;
  return String(right.id).localeCompare(String(left.id));
}

export function sceneTimeLabel(scene) {
  const value = sceneTimeValue(scene);
  if (CLOCK.test(value) && parseSourceTime(value)) return formatSourceTime(value, { includeYear: true });
  return sceneDay(scene) || "时间未记录";
}
