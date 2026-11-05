import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, test } from 'vitest';

import App from './App';

test('announces the workspace preview and provides keyboard access to main content', async () => {
  const user = userEvent.setup();
  render(<App />);

  const main = screen.getByRole('main');
  expect(
    within(main).getByRole('heading', { level: 1, name: 'Job Hunter' }),
  ).toBeVisible();
  expect(
    within(main).getByRole('region', {
      name: 'A new workspace is taking shape',
    }),
  ).toHaveTextContent('Profile and search tools are coming soon.');

  const skipLink = screen.getByRole('link', { name: 'Skip to main content' });
  expect(skipLink).toHaveAttribute('href', `#${main.id}`);
  expect(main).toHaveAttribute('tabindex', '-1');
  await user.tab();
  expect(skipLink).toHaveFocus();
  await user.keyboard('{Enter}');
  await waitFor(() => {
    expect(window.location.hash).toBe('#main-content');
  });
});
