import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Button, StatePanel } from '../../shared/ui';
import { SearchEditor, SearchLogic, type SavedSearch } from './SearchEditor';

export function Searches({ ownerId }: { ownerId: string }) {
  const key = ['searches', ownerId] as const;
  const [offset, setOffset] = useState(0), [notice, setNotice] = useState('');
  const [editor, setEditor] = useState<{ saved: SavedSearch | null } | null>(null);
  const [removing, setRemoving] = useState<SavedSearch | null>(null);
  const opener = useRef<HTMLElement | null>(null), mounted = useRef(false), busy = useRef(false);
  const request = useRef<AbortController | null>(null);
  const list = useQuery({ queryKey: [...key, offset], queryFn: ({ signal }) => api.request<components['schemas']['SearchPage']>(`/api/v1/searches?limit=20&offset=${String(offset)}`, { signal }) });
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); }; }, []);
  const deletion = useMutation({ mutationFn: (value: SavedSearch) => {
    const controller = new AbortController(); request.current = controller;
    return api.request<undefined>(`/api/v1/searches/${encodeURIComponent(value.id)}?expected_revision=${String(value.revision)}`, { method: 'DELETE', signal: controller.signal });
  }, onSuccess: async () => {
    await api.queryClient.invalidateQueries({ queryKey: key });
    if (mounted.current) { setRemoving(null); setNotice('Search deleted. Existing runs are kept.'); }
  }, onSettled: () => { busy.current = false; } });
  useLayoutEffect(() => { if (removing) document.getElementById('search-keep')?.focus(); }, [removing]);
  useLayoutEffect(() => {
    if (removing || editor || !opener.current || list.isFetching || deletion.isPending) return;
    (opener.current.isConnected ? opener.current : document.getElementById('search-new'))?.focus(); opener.current = null;
  }, [editor, removing, list.isFetching, deletion.isPending]);
  const inaccessible = [list.error, deletion.error].some(error => error instanceof ApiError && (error.status === 401 || error.status === 403));
  useEffect(() => { if (inaccessible) void api.invalidateSession(); }, [inaccessible]);
  async function refresh() { const result = await list.refetch(); if (result.isError) throw result.error; }
  async function reload() {
    try { await refresh(); if (mounted.current) { deletion.reset(); setRemoving(null); setNotice('Searches reloaded. Review the latest version before deleting.'); } }
    catch { if (mounted.current) setNotice('The list could not be reloaded. Try again.'); }
  }
  const blocked = list.isFetching || list.isError || deletion.isPending || Boolean(editor || removing);
  return <div className="search-content">
    <p>Organize words and phrases into reusable discovery hints. These are separate from filters on verified job details.</p>
    <Button id="search-new" disabled={blocked} onClick={event => { opener.current = event.currentTarget; setEditor({ saved: null }); setNotice(''); }}>New search</Button>
    <p role="status">{notice}</p>
    {list.isError && <StatePanel state={list.data ? 'stale' : 'error'} title="Saved searches could not be loaded"
      description="Your open draft is kept. Reload to see the latest saved searches."
      action={<Button loading={list.isFetching} onClick={() => { void reload(); }}>Reload searches</Button>} />}
    {!list.data && !list.isError && <StatePanel state="loading" title="Loading saved searches" description="Getting your private search definitions." />}
    {list.data && <>
      {list.data.items.length === 0 && <StatePanel state="empty" title={offset ? 'No searches on this page' : 'No saved searches yet'} description="Create a named search with words or phrases you want to discover." />}
      <ul className="search-list">{list.data.items.map(value => <li key={value.id}>
        <h2>{value.name}</h2><SearchLogic definition={value.definition} />
        <div className="search-actions"><Button tone="secondary" disabled={blocked} aria-label={`Edit ${value.name}`} onClick={event => { opener.current = event.currentTarget; setEditor({ saved: value }); setNotice(''); }}>Edit</Button>
          <Button tone="secondary" disabled={blocked} aria-label={`Delete ${value.name}`} onClick={event => { opener.current = event.currentTarget; setRemoving(value); deletion.reset(); setNotice(''); }}>Delete</Button></div>
      </li>)}</ul>
      <nav className="search-actions" aria-label="Saved search pages">
        <Button tone="secondary" disabled={blocked || offset === 0} onClick={() => { setOffset(Math.max(0, offset - 20)); }}>Previous searches</Button>
        <Button tone="secondary" disabled={blocked || list.data.next_offset == null} onClick={() => { setOffset(list.data.next_offset ?? offset); }}>Next searches</Button>
      </nav>
    </>}
    {removing && <div className="search-confirm">
      <p>Delete “{removing.name}”? Existing run history is kept.</p>
      {deletion.error && <p role="alert">Deletion could not be confirmed, or this search changed elsewhere. Reload before confirming again.
        {' '}<Button loading={list.isFetching} onClick={() => { void reload(); }}>Reload before deleting</Button></p>}
      <div className="search-actions"><Button id="search-keep" tone="secondary" disabled={deletion.isPending} onClick={() => { setRemoving(null); deletion.reset(); }}>Keep search</Button>
        <Button tone="danger" disabled={deletion.isPending || Boolean(deletion.error)} loading={deletion.isPending} loadingLabel="Deleting search…" onClick={() => {
          if (busy.current) return; busy.current = true; deletion.mutate(removing);
        }}>Confirm deletion</Button></div>
    </div>}
    {editor && <SearchEditor key={editor.saved?.id ?? 'new'} initial={editor.saved} refreshList={refresh} close={() => { setEditor(null); }} saved={async value => {
      await api.queryClient.invalidateQueries({ queryKey: key });
      if (mounted.current) { setEditor(null); setNotice(`Saved “${value.name}”.`); }
    }} />}
  </div>;
}
