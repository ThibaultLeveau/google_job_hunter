import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { http, HttpResponse } from 'msw';
import { afterAll, afterEach, beforeAll, expect } from 'vitest';

export const server = setupServer(
  http.get('http://localhost/api/auth/methods', () => HttpResponse.json({ methods: ['password'], auth_method: 'password', authenticated_at: null, recent: false, recency_seconds: 300 })),
  http.get('http://localhost/api/v1/searches/runs', () => HttpResponse.json({ items: [], next_offset: null })),
  http.get('http://localhost/api/v1/documents', () => HttpResponse.json([])),
  http.get('http://localhost/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
);
const unhandledRequests: string[] = [];

beforeAll(() => {
  server.listen({
    onUnhandledFrame({ frame }) {
      const message = `Unhandled ${frame.protocol} network request`;
      unhandledRequests.push(message);
      throw new Error(message);
    },
  });
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  window.history.replaceState(null, '', '/');
  // A component catching a request error must not hide unexpected network traffic.
  expect(unhandledRequests.splice(0), 'Unhandled network requests').toEqual([]);
});

afterAll(() => {
  server.close();
  expect(unhandledRequests, 'Late unhandled network requests').toEqual([]);
});
