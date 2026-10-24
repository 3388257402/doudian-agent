import { useCallback, useEffect, useRef, useState } from "react";
import { ApiFailure, toApiFailure } from "../api/errors";

export interface ResourceOptions<T> {
  /** 变更即重新拉取（把它当成依赖数组的字符串化形式）。 */
  key: string;
  fetcher: (signal: AbortSignal) => Promise<T>;
  /** 轮询间隔；null/undefined = 不轮询。页面不可见时暂停。 */
  intervalMs?: number | null;
  enabled?: boolean;
}

export interface ResourceState<T> {
  data: T | null;
  error: ApiFailure | null;
  /** 首次加载（还没有任何数据） */
  loading: boolean;
  /** 有数据但正在后台刷新 */
  refreshing: boolean;
  updatedAt: number | null;
  refresh: () => void;
}

/**
 * 统一的数据获取 + 轮询。
 * 关键行为（每一条都是踩过的坑）：
 * 1. 请求序列号守卫：晚到的旧响应不许覆盖新响应（快速切筛选时不会串数据）。
 * 2. 卸载/换 key 时 abort 上一个请求，避免 setState-after-unmount 与浪费的上游调用。
 * 3. 页面 hidden 时跳过轮询，回到前台立刻补一次。
 * 4. ABORTED 不算错误（那是我们自己取消的），不弹错误横幅。
 */
export function useResource<T>(options: ResourceOptions<T>): ResourceState<T> {
  const { key, intervalMs, enabled = true } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiFailure | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  const fetcherRef = useRef(options.fetcher);
  fetcherRef.current = options.fetcher;

  const seqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => {
    setTick((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const seq = ++seqRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setError(null);
    setLoading((prev) => prev || data === null);
    setRefreshing(data !== null);

    fetcherRef.current(controller.signal)
      .then((result) => {
        if (seq !== seqRef.current) return;
        setData(result);
        setUpdatedAt(Date.now());
        setError(null);
      })
      .catch((err: unknown) => {
        if (seq !== seqRef.current) return;
        const failure = toApiFailure(err);
        if (failure.code === "ABORTED") return;
        setError(failure);
      })
      .finally(() => {
        if (seq !== seqRef.current) return;
        setLoading(false);
        setRefreshing(false);
      });

    return () => {
      controller.abort();
    };
    // data 只用于判断"首次 vs 刷新"，不参与依赖，避免每次成功都重跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, tick]);

  useEffect(() => {
    if (!enabled || intervalMs === null || intervalMs === undefined) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      setTick((n) => n + 1);
    }, intervalMs);
    const onVisible = (): void => {
      if (document.visibilityState === "visible") setTick((n) => n + 1);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, intervalMs]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  return { data, error, loading, refreshing, updatedAt, refresh };
}
