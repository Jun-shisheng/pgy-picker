import { useRef, useState, type Dispatch, type SetStateAction } from "react";

/**
 * 切换左侧模块会卸载页面，普通 useState 一回来就归零。
 * 用于「用户希望保持离开时那样」的轻量 UI 状态（页码、搜索词这类）：
 * 存在 sessionStorage —— 同一次客户端会话内切换模块保持，重开客户端才重置。
 */
export function usePersistentState<T>(key: string, initial: T): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.sessionStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const latest = useRef(value);
  const update: Dispatch<SetStateAction<T>> = next => {
    const resolved = typeof next === "function" ? (next as (previous: T) => T)(latest.current) : next;
    latest.current = resolved;
    setValue(resolved);
    try {
      window.sessionStorage.setItem(key, JSON.stringify(resolved));
    } catch {
      // 隐私模式或配额满：退化成普通内存状态，不影响功能。
    }
  };
  return [value, update];
}
