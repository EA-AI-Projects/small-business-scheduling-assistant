import type { NextPage } from "next";
import type { AppProps } from "next/app";
import Head from "next/head";
import type { ReactNode } from "react";

import "@/styles/globals.css";

type PageWithLayout = NextPage & { getLayout?: (page: ReactNode) => ReactNode };

export default function OwnerApp({ Component, pageProps }: AppProps) {
  // A page may export `getLayout` so shared chrome (the client shell) stays mounted between pages.
  const getLayout = (Component as PageWithLayout).getLayout ?? ((page: ReactNode) => page);
  return (
    <>
      <Head>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Smart Scheduling Assistant · Owner</title>
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
      </Head>
      {getLayout(<Component {...pageProps} />)}
    </>
  );
}
