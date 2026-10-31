/**
 * GuideEntry — the AI expert guide's doorway at the top of
 * onboarding (the dashboard, next to the setup checklist). The guide
 * is a layer, not a replacement: the copy says the self-serve
 * checklist stays fully available, and the guide page itself renders
 * the honest unavailable state when no AI key is configured.
 */
import React from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { Bot } from 'lucide-react';
import { Card } from '@/components/Page';
import { Button } from '@/components/ui/button';

export default function GuideEntry() {
  const { t } = useTranslation();
  return (
    <Card className="p-5 mb-4 flex items-center justify-between gap-4" data-testid="guide-entry">
      <div className="flex items-start gap-3">
        <Bot size={22} className="text-amber-400 mt-0.5 shrink-0" />
        <div>
          <p className="font-medium text-gray-100">{t('guide.entryTitle')}</p>
          <p className="text-sm text-gray-500">{t('guide.entryBody')}</p>
        </div>
      </div>
      <Link to="/setup-guide">
        <Button data-testid="guide-entry-button">{t('guide.entryButton')}</Button>
      </Link>
    </Card>
  );
}
