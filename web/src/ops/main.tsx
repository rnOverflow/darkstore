import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../shared/base.css';
import './ops.css';
import { OpsApp } from './OpsApp';

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <OpsApp />
    </StrictMode>,
  );
}
