import { useEffect, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { ApiError } from '../../shared/api';

export function useAuthAction<T>(perform: () => Promise<T>) {
  const busy = useRef(false);
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(Date.now);
  const mutation = useMutation({ mutationFn: perform, onError: error => {
    if (error instanceof ApiError && error.status === 429) {
      const delay = Number.isFinite(error.retryAfterMs) && error.retryAfterMs > 0 ? error.retryAfterMs : 60000;
      setNow(Date.now()); setRetryAt(Date.now() + delay);
    }
  }, onSettled: () => { busy.current = false; } });
  const wait = Math.max(0, Math.ceil((retryAt - now) / 1000));
  useEffect(() => {
    if (wait === 0) return;
    const timer = window.setTimeout(() => { setNow(Date.now()); }, 1000);
    return () => { window.clearTimeout(timer); };
  }, [retryAt, now, wait]);
  return { ...mutation, wait, run: () => {
    if (busy.current || Date.now() < retryAt) return;
    busy.current = true; mutation.mutate();
  } };
}

export function authError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return 'Too many attempts. Wait before trying again.';
    if (error.status === 401) return 'Check your email and password, then sign in again.';
    if (error.status === 422) return 'Check the marked fields and try again.';
    if (error.kind === 'network') return 'Could not connect. Check your connection and try again.';
  }
  return 'The request could not be completed. Please try again.';
}

export function fieldError(error: unknown, field: string) {
  return error instanceof ApiError
    ? error.detail?.field_errors?.find(item => item.field === field)?.message : undefined;
}
