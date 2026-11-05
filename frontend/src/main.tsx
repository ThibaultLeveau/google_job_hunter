import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClientProvider } from '@tanstack/react-query';

import App from './App';
import { api } from './shared/api';
import './styles.css';

const root = document.getElementById('root');

if (!root) {
  throw new Error('The application root is missing.');
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={api.queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
