import type { SidebarsConfig } from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    'index',
    {
      type: 'category',
      label: 'Install',
      items: ['install'],
    },
    {
      type: 'category',
      label: 'Usage',
      items: ['usage'],
    },
  ],
};

export default sidebars;
