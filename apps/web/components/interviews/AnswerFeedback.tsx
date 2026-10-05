import type { InterviewScore } from '@/lib/interviews/types';
import styles from './interviews.module.css';

export function AnswerFeedback({ score }: { score: InterviewScore }) {
  const hasFeedback = !!(score.weaknesses?.length || score.actionItems?.length || score.followUp);
  if (!hasFeedback && !score.feedbackWarning) return null;
  return <section className={styles.feedback} aria-label="Answer improvement feedback">
    <h4>Fix this answer</h4>
    <p className={styles.feedbackProvenance}>Grades: {score.gradingSource || 'Source not supplied'} · Feedback: {score.feedbackSource || 'Source not supplied'}</p>
    {score.feedbackWarning && <p className={styles.warning}>{score.feedbackWarning}</p>}
    {!!score.weaknesses?.length && <><h5>What is missing</h5><ul>{score.weaknesses.map((weakness, index) => <li key={index}>{weakness}</li>)}</ul></>}
    {!!score.actionItems?.length && <><h5>Your next attempt</h5><ol>{score.actionItems.map((action, index) => <li key={index}>{action}</li>)}</ol></>}
    {score.followUp && <><h5>Be ready to answer</h5><blockquote>{score.followUp}</blockquote></>}
  </section>;
}
