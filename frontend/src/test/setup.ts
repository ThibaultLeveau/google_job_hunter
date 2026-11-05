import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, expect } from 'vitest';

export const server = setupServer();
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
