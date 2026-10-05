import type { Metadata } from 'next';
import { InterviewsView } from '@/components/interviews/InterviewsView';
export const metadata: Metadata = { title: 'Interviews · Interviewmaxxing' };
export default function InterviewsPage() { return <InterviewsView />; }
