export default function App() {
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <main id="main-content" tabIndex={-1}>
        <p className="eyebrow">Your next chapter</p>
        <h1>Job Hunter</h1>
        <p className="introduction">
          A private workspace for your profile and job research.
        </p>
        <section aria-labelledby="workspace-status">
          <h2 id="workspace-status">A new workspace is taking shape</h2>
          <p>
            Profile and search tools are coming soon. This preview is the first
            step toward researching your next role.
          </p>
        </section>
      </main>
    </>
  );
}
