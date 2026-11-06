import { QueryClient, queryOptions, type Query } from '@tanstack/react-query';
import type { components, paths } from '../../generated/api';

export type Session = paths['/api/auth/me']['get']['responses'][200]['content']['application/json'];
type Envelope = components['schemas']['ErrorEnvelope'];
type FieldError = components['schemas']['FieldError'];
type ErrorKind = 'unauthenticated' | 'validation' | 'retryable' | 'http' | 'network' | 'invalid_response';

export class ApiError extends Error {
  constructor(
    readonly kind: ErrorKind,
    readonly status = 0,
    readonly detail?: Envelope,
    readonly retryAfterMs = 0,
  ) {
    super(detail?.message ?? 'The request could not be completed.');
    this.name = 'ApiError';
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function fieldError(value: unknown): value is FieldError {
  return record(value) && typeof value.field === 'string' && typeof value.message === 'string';
}

function envelope(value: unknown): Envelope | undefined {
  if (!record(value) || typeof value.code !== 'string' || typeof value.message !== 'string'
    || typeof value.request_id !== 'string' || !Array.isArray(value.field_errors)
    || !value.field_errors.every(fieldError)) return undefined;
  return value as Envelope;
}

function retryAfter(value: string | null) {
  if (value === null) return 0;
  const milliseconds = /^\d+$/.test(value) ? Number(value) * 1000 : Date.parse(value) - Date.now();
  return Number.isNaN(milliseconds) ? 0 : Math.max(0, milliseconds);
}

type RequestOptions = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
  upload?: { file: File; mediaType: string };
};
type ResponseData<T> = { data: T; status: number };

export const sessionKey = ['session'] as const;
const privateQueries = { predicate: (query: Query) => query.queryKey.length !== 1 || query.queryKey[0] !== sessionKey[0] };

export function createApiClient() {
  const queryClient = new QueryClient({ defaultOptions: {
    queries: {
      retry: (count, error) => count < 2 && error instanceof ApiError
        && (error.kind === 'network' || error.kind === 'retryable') && error.retryAfterMs <= 30000,
      retryDelay: (attempt, error) => Math.max(1000 * 2 ** attempt, error instanceof ApiError ? error.retryAfterMs : 0),
    },
    mutations: { retry: false },
  } });
  let session: Session | null = null;
  let generation = 0;
  const pending = new Set<AbortController>();

  function discardPrivateData() {
    generation++;
    for (const controller of pending) controller.abort();
    void queryClient.cancelQueries(privateQueries);
    queryClient.removeQueries(privateQueries);
    queryClient.getMutationCache().clear();
  }

  function clearSession() {
    acceptSession(null);
  }

  function acceptSession(next: Session | null) {
    session = next;
    discardPrivateData();
    void queryClient.cancelQueries({ queryKey: sessionKey, exact: true });
    queryClient.setQueryData(sessionKey, next);
  }

  function request<T>(path: `/api/${string}`, options: RequestOptions & { withStatus: true }): Promise<ResponseData<T>>;
  function request<T>(path: `/api/${string}`, options?: RequestOptions): Promise<T>;
  async function request(path: `/api/${string}`, { method = 'GET', body, signal, upload, withStatus }: RequestOptions & { withStatus?: boolean } = {}): Promise<unknown> {
    const url = new URL(path, window.location.origin);
    if (!path.startsWith('/api/') || url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) {
      throw new TypeError('API requests must stay within the same-origin /api boundary.');
    }
    const controller = new AbortController();
    const abort = () => { controller.abort(signal?.reason); };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    pending.add(controller);
    const headers = new Headers({ Accept: 'application/json' });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    if (method !== 'GET' && session) headers.set('X-CSRF-Token', session.csrf_token);
    try {
      if (upload) {
        headers.set('Content-Type', upload.mediaType);
        headers.set('X-Document-Name', encodeURIComponent(upload.file.name));
      }
      const response = await fetch(url, {
        method, headers, credentials: 'same-origin', redirect: 'error', cache: 'no-store',
        signal: controller.signal, ...(upload ? { body: upload.file } : body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const data: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined);
      controller.signal.throwIfAborted();
      if (!response.ok) {
        const kind = response.status === 401 ? 'unauthenticated' : response.status === 422 ? 'validation'
          : response.status === 408 || response.status === 429 || response.status >= 500 ? 'retryable' : 'http';
        throw new ApiError(kind, response.status, envelope(data), retryAfter(response.headers.get('Retry-After')));
      }
      if (data === undefined && response.status !== 204) throw new ApiError('invalid_response', response.status);
      return withStatus ? { data, status: response.status } : data;
    } catch (error) {
      controller.signal.throwIfAborted();
      if (error instanceof ApiError || (record(error) && error.name === 'AbortError')) throw error;
      throw new ApiError('network');
    } finally {
      signal?.removeEventListener('abort', abort);
      pending.delete(controller);
    }
  }

  const currentSession = queryOptions({
    queryKey: sessionKey,
    queryFn: async ({ signal }): Promise<Session | null> => {
      let next: Session | null;
      try {
        next = await request<Session>('/api/auth/me', { signal });
      } catch (error) {
        if (!(error instanceof ApiError) || error.kind !== 'unauthenticated') throw error;
        next = null;
      }
      signal.throwIfAborted();
      // Keep this session query alive while discarding data from the previous identity.
      if (next === null || session?.id !== next.id || session.csrf_token !== next.csrf_token) discardPrivateData();
      session = next;
      return next;
    },
  });

  async function logout() {
    const started = generation;
    try {
      await request<undefined>('/api/auth/logout', { method: 'POST' });
    } finally {
      if (started === generation) clearSession();
    }
  }

  return { queryClient, request, currentSession, clearSession, acceptSession, logout,
    invalidateSession: () => queryClient.invalidateQueries({ queryKey: sessionKey, exact: true }) };
}

export const api = createApiClient();
