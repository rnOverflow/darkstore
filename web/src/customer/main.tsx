import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../shared/base.css';
import './customer.css';
import { App } from './App';

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
