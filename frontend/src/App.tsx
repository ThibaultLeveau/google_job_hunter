import { BrowserRouter } from 'react-router';
import { AppErrorBoundary, Shell } from './app/Shell';

export default function App() {
  return <BrowserRouter><AppErrorBoundary><Shell /></AppErrorBoundary></BrowserRouter>;
}
