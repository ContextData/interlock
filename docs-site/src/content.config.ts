import { defineCollection, z } from 'astro:content';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

export const collections = {
  docs: defineCollection({
    loader: docsLoader(),
    schema: docsSchema({
      extend: z.object({
        // Contracts, runbooks and the connector matrix are normative: what
        // they say is what the project promises. Tests pin their phrasing.
        normative: z.boolean().optional(),
        // Pages written by tools/docs/generate.py; never edit them by hand.
        generated: z.boolean().optional(),
      }),
    }),
  }),
};
