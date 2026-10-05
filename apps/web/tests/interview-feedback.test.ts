import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AnswerFeedback } from '@/components/interviews/AnswerFeedback';

describe('answer feedback presentation', () => {
  it('renders concrete weaknesses, drills and follow-up with separate grade/feedback provenance', () => {
    const html = renderToStaticMarkup(createElement(AnswerFeedback, { score: {
      weaknesses: ['You gave no baseline for the 30% growth claim.'],
      actionItems: ['State the starting conversion rate and test window.'],
      followUp: 'How did you separate your campaign from seasonality?',
      gradingSource: 'Jev Decisions API', feedbackSource: 'anthropic/claude-sonnet-4.6',
    } }));
    expect(html).toContain('Fix this answer');
    expect(html).toContain('You gave no baseline for the 30% growth claim.');
    expect(html).toContain('State the starting conversion rate and test window.');
    expect(html).toContain('How did you separate your campaign from seasonality?');
    expect(html).toContain('Grades: Jev Decisions API');
    expect(html).toContain('Feedback: anthropic/claude-sonnet-4.6');
  });
  it('does not invent guidance when absent, and explains feedback-provider failure', () => {
    expect(renderToStaticMarkup(createElement(AnswerFeedback, {score: {overallScore: 50}}))).toBe('');
    const html = renderToStaticMarkup(createElement(AnswerFeedback, {score: {
      feedbackWarning: 'Detailed feedback unavailable; rubric drills retained.',
      gradingSource: 'Jev Decisions API', feedbackSource: 'rubric', actionItems: ['Add a measured baseline.'],
    }}));
    expect(html).toContain('Detailed feedback unavailable; rubric drills retained.');
    expect(html).toContain('Feedback: rubric');
  });
});
