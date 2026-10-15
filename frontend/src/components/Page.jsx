import { useTranslation } from "react-i18next";
export function PageHeader({
  title,
  subtitle,
  children,
  testid
}) {
  return <div className="flex flex-wrap items-end justify-between gap-4 mb-6" data-testid={testid}>
      <div>
        <h1 className="font-display text-2xl sm:text-3xl font-bold tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-gray-500 mt-1">{subtitle}</p>}
      </div>
      {children && <div className="flex items-center gap-2">{children}</div>}
    </div>;
}
export function Card({
  className = "",
  children,
  ...rest
}) {
  return <div className={`card-surface p-5 sm:p-6 ${className}`} {...rest}>
      {children}
    </div>;
}
export function Empty({
  children
}) {
  return <div className="text-center py-16 text-gray-500 text-sm">{children}</div>;
}
export function Loading() {
  const {
    t
  } = useTranslation();
  return <div className="py-16 text-center text-amber-500/80 font-display animate-pulse">{t("page.loading")}</div>;
}
