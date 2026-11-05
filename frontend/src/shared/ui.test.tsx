import { readFileSync } from 'node:fs';
import { createRef, useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, test, vi } from 'vitest';

import { Badge, Button, Field, Input, StatePanel } from './ui';
const css = readFileSync('src/styles.css', 'utf8');

test('labels fields, associates hints and errors, and preserves native input attributes/ref', async () => {
  const user = userEvent.setup();
  const ref = createRef<HTMLInputElement>();
  const { rerender } = render(<>
    <p id="policy">Kept private.</p>
    <Field label="Email" id="email" ref={ref} type="email" required autoComplete="email"
      aria-describedby="policy" description="Use a current address." error="Check the address." />
    <Field label="Disabled field" disabled />
    <Button tone="secondary">Continue</Button>
  </>);
  const input = screen.getByRole('textbox', { name: 'Email' });
  expect(ref.current).toBe(input);
  expect(input).toBeRequired();
  expect(input).toHaveAttribute('autocomplete', 'email');
  expect(input).toBeInvalid();
  expect(input).toHaveAccessibleDescription('Kept private. Use a current address. Error: Check the address.');
  await user.tab();
  expect(input).toHaveFocus();
  await user.type(input, 'person@example.org');
  expect(input).toHaveValue('person@example.org');
  await user.tab();
  expect(screen.getByRole('button', { name: 'Continue' })).toHaveFocus();
  rerender(<Field label="Email" />);
  expect(screen.getByRole('textbox')).not.toHaveAttribute('aria-invalid');
  expect(screen.getByRole('textbox')).not.toHaveAttribute('aria-describedby');
});

test('generates distinct label targets and allows a standalone labeled input', () => {
  render(<><Field label="First" /><Field label="Second" /><Input aria-label="Filter" className="filter" readOnly /></>);
  expect(screen.getByLabelText('First').id).not.toBe(screen.getByLabelText('Second').id);
  expect(screen.getByLabelText('Filter')).toHaveAttribute('readonly');
  expect(screen.getByLabelText('Filter')).toHaveClass('filter');
});

test('defaults to a safe non-submit button and blocks loading/disabled activation', async () => {
  const user = userEvent.setup();
  const click = vi.fn();
  const submit = vi.fn();
  const ref = createRef<HTMLButtonElement>();
  const { rerender } = render(<form onSubmit={event => { event.preventDefault(); submit(); }}>
    <Button ref={ref} onClick={click} tone="danger" className="remove">Remove</Button>
  </form>);
  await user.tab();
  expect(ref.current).toHaveFocus();
  await user.keyboard('{Enter}');
  expect(click).toHaveBeenCalledOnce();
  expect(submit).not.toHaveBeenCalled();
  expect(ref.current).toHaveClass('remove');
  rerender(<Button loading onClick={click}>Remove</Button>);
  const loading = screen.getByRole('button', { name: 'Working…' });
  expect(loading).toHaveAttribute('aria-busy', 'true');
  expect(loading).toBeDisabled();
  await user.click(loading);
  rerender(<Button disabled onClick={click}>Unavailable</Button>);
  await user.click(screen.getByRole('button'));
  expect(click).toHaveBeenCalledOnce();
  rerender(<Button onClick={click}>Retry</Button>);
  await user.click(screen.getByRole('button'));
  expect(click).toHaveBeenCalledTimes(2);
});

test('prevents repeated pointer and implicit keyboard submits while a form is pending', async () => {
  const user = userEvent.setup();
  const submit = vi.fn();
  function PendingForm() {
    const [loading, setLoading] = useState(false);
    return <form onSubmit={event => { event.preventDefault(); setLoading(true); submit(); }}>
      <Field label="Title" />
      <Button type="submit" loading={loading} loadingLabel="Saving…">Save</Button>
    </form>;
  }
  render(<PendingForm />);
  await user.dblClick(screen.getByRole('button', { name: 'Save' }));
  await user.click(screen.getByLabelText('Title'));
  await user.keyboard('{Enter}{Enter}');
  expect(submit).toHaveBeenCalledOnce();
  expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled();
});

test.each(['loading', 'empty', 'error', 'unsupported', 'unknown', 'stale'] as const)(
  'announces the %s state with visible text and a keyboard-operable action', async state => {
    const user = userEvent.setup();
    const retry = vi.fn();
    render(<StatePanel state={state} title="Result status" description="Try another source."
      action={<Button onClick={retry}>Try again</Button>} />);
    const panel = screen.getByRole('region', { name: 'Result status' });
    const announcement = within(panel).getByRole(state === 'error' ? 'alert' : 'status');
    expect(announcement).toHaveTextContent('Try another source.');
    expect(within(panel).getByRole('heading', { level: 2 })).toBeVisible();
    await user.tab();
    expect(within(panel).getByRole('button')).toHaveFocus();
    await user.keyboard(' ');
    expect(retry).toHaveBeenCalledOnce();
  },
);

test('renders external text literally and supports informational states without actions', () => {
  const external = '<img src=x onerror=alert(1)>';
  const { container } = render(<><Badge tone="accent">Verified</Badge><Badge>Not reviewed</Badge>
    <StatePanel state="unknown" title={external} description={external} />
    <Field label={external} error={external} />
  </>);
  expect(screen.getByText('Verified')).toBeVisible();
  expect(screen.getByText('Not reviewed')).toBeVisible();
  expect(screen.getByRole('region')).toHaveAccessibleName(external);
  expect(container.querySelector('img')).toBeNull();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

function luminance(name: string) {
  const hex = css.match(new RegExp(`--color-${name}: #(\\w{6});`))?.[1];
  if (!hex) throw new Error(`Missing color token: ${name}`);
  const channels = [0, 2, 4].map(offset => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return channels.reduce((sum, channel, index) => sum + channel * ([0.2126, 0.7152, 0.0722][index] ?? 0), 0);
}

test.each([
  ['text', 'canvas', 4.5], ['text', 'surface', 4.5], ['text', 'disabled', 4.5],
  ['muted', 'surface', 4.5], ['muted', 'canvas', 4.5], ['muted', 'disabled', 4.5],
  ['surface', 'accent', 4.5], ['surface', 'accent-hover', 4.5], ['accent', 'accent-soft', 4.5],
  ['surface', 'danger', 4.5], ['surface', 'danger-hover', 4.5], ['danger', 'danger-soft', 4.5],
  ['danger', 'surface', 4.5], ['border', 'surface', 3], ['border', 'canvas', 3],
  ['accent', 'surface', 3], ['accent', 'canvas', 3],
] as const)('checks %s on %s contrast without rounding', (foreground, background, minimum) => {
  const a = luminance(foreground), b = luminance(background);
  expect((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)).toBeGreaterThanOrEqual(minimum);
});
