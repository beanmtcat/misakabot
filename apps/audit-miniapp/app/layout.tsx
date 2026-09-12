import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';

const geistSans = Geist({ variable: '--font-geist-sans', subsets: ['latin'] });
const geistMono = Geist_Mono({ variable: '--font-geist-mono', subsets: ['latin'] });

export const metadata: Metadata = {
  metadataBase: new URL('https://misaka-guard-audit-20260829.htc10.chatgpt.site'),
  title: 'Misaka Guard · 群组审计中心',
  description: 'Telegram 群组广告拦截与自动化账号审计记录。',
  openGraph: {
    title: 'Misaka Guard · 群组审计中心',
    description: 'Telegram 群组广告拦截与自动化账号审计记录。',
    images: [{ url: '/og.png', width: 1536, height: 864, alt: 'Misaka Guard 群组审计中心' }],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Misaka Guard · 群组审计中心',
    description: 'Telegram 群组广告拦截与自动化账号审计记录。',
    images: ['/og.png'],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN">
    <head>
      <script src="https://telegram.org/js/telegram-web-app.js" />
    </head>
    <body className={`${geistSans.variable} ${geistMono.variable} antialiased`}>{children}</body>
  </html>;
}
