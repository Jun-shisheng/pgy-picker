import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, Loader2, RotateCcw, Save } from "lucide-react";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { api, type PricingConfig, type PricingTier } from "../../lib/api";

/**
 * 一个量级这次取不取、取几条。
 *
 * ⚠️ **这是"每次都可能不同"的检索条件，不落盘**；价格和倍数是长期配置，
 * 会通过「保存档位」写进本机 `pricing.json`。两者放在同一张表里，
 * 但只有价格会被保存（用户 2026-11-19 要求把报价表整体搬到选号页）。
 */
export interface TierSetting {
  key: string;
  enabled: boolean;
  count: number;
}

const DEFAULT_COUNT = 20;
const tierKey = (tier: PricingTier) => `${tier.label}(${tier.bracket})`;

/**
 * 报价档位、预算上限与每档条数。
 *
 * 这张表原来在「设置」页；用户 2026-11-19 要求**整体搬到选号页**，
 * 并在每行加上「勾选」和「取多少条」——每次选号前当场确认。
 *
 * - 区间/名称固定（选号分档是 `fields.TIERS`，改区间会让预算算错）；
 * - 可改每个档位的图文/视频基准价 + 倍数，保存到 `<数据目录>\pricing.json`；
 * - 勾选与条数只对本次检索生效，不保存。
 */
export function PricingTiersCard({
  tierSettings,
  onTierSettingsChange,
  disabled = false,
  onSaved,
}: {
  tierSettings: TierSetting[];
  onTierSettingsChange: React.Dispatch<React.SetStateAction<TierSetting[]>>;
  disabled?: boolean;
  /** 保存成功后通知外面刷新选号前置状态（顶部「报价档位：已配置」）。 */
  onSaved?: () => void;
}) {
  const qc = useQueryClient();
  const [config, setConfig] = useState<PricingConfig | null>(null);
  const [multiplier, setMultiplier] = useState("1.3");
  const [pricingTiers, setPricingTiers] = useState<PricingTier[]>([]);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const apply = (data: PricingConfig) => {
    setConfig(data);
    setMultiplier(String(data.multiplier));
    setPricingTiers(data.tiers ?? []);
  };

  const load = async () => {
    setLoading(true);
    setMessage("");
    try {
      apply(await api.getPricing());
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  // 档位清单到了以后补全勾选/条数；**保留用户已经改过的值**。
  useEffect(() => {
    if (pricingTiers.length === 0) return;
    onTierSettingsChange((prev) => pricingTiers.map((tier) => {
      const key = tierKey(tier);
      return prev.find((item) => item.key === key)
        ?? { key, enabled: true, count: DEFAULT_COUNT };
    }));
  }, [pricingTiers, onTierSettingsChange]);

  const setPrice = (index: number, key: "image" | "video", raw: string) => {
    const value = Math.max(0, Math.floor(Number(raw) || 0));
    setPricingTiers((prev) => prev.map((tier, i) => (
      i === index ? { ...tier, [key]: value } : tier
    )));
  };

  const withDefaults = (prev: TierSetting[]): TierSetting[] => pricingTiers.map((tier) => {
    const key = tierKey(tier);
    return prev.find((item) => item.key === key)
      ?? { key, enabled: true, count: DEFAULT_COUNT };
  });

  const setSetting = (key: string, patch: Partial<TierSetting>) => {
    onTierSettingsChange((prev) => withDefaults(prev).map((item) => (
      item.key === key ? { ...item, ...patch } : item
    )));
  };

  const selectAll = (enabled: boolean) => {
    onTierSettingsChange((prev) => withDefaults(prev).map((item) => ({ ...item, enabled })));
  };

  const factor = Number(multiplier) || 0;
  const cap = (base: number) => Math.round(base * factor);
  const money = (value: number) => (value ? value.toLocaleString("zh-CN") : "—");

  const enabledSettings = tierSettings.filter((item) => item.enabled);
  const plannedCount = enabledSettings.reduce((sum, item) => sum + (Number(item.count) || 0), 0);

  const save = async () => {
    setSaving(true);
    setMessage("");
    try {
      const data = await api.savePricing({
        multiplier: Number(multiplier) || 0,
        tiers: pricingTiers,
      });
      apply(data);
      setMessage("已保存。下一次检索会按这套上限做预算把关。");
      // 选号页的「报价档位」状态要跟着变（未配置 → 已配置）。
      void qc.invalidateQueries({ queryKey: ["selection-status"] });
      onSaved?.();
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const restoreDefault = async () => {
    setMessage("");
    try {
      const data = await api.getDefaultPricing();
      setMultiplier(String(data.multiplier));
      setPricingTiers(data.tiers);
      setMessage("已载入开发期默认值；点「保存档位」后生效。");
    } catch (error) {
      setMessage((error as Error).message);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>报价档位、预算与每档条数</CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary" className="h-8 px-3 text-xs"
            onClick={() => selectAll(true)} disabled={disabled || tierSettings.length === 0}
          >
            全选
          </Button>
          <Button
            variant="secondary" className="h-8 px-3 text-xs"
            onClick={() => selectAll(false)} disabled={disabled || tierSettings.length === 0}
          >
            全不选
          </Button>
          <Button
            variant="secondary" className="h-8 px-3 text-xs"
            onClick={() => void load()} disabled={loading || disabled}
          >
            <RotateCcw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /> 重新读取
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs leading-relaxed text-text-secondary">
          报价上限 = 对应档位的<b>基准价 × 倍数</b>。图文、视频<b>任一</b>不超上限即通过；
          两个都超才剔除。没配置时预算把关是关着的（超预算达人会照样进名单）。
          <br />
          <b>勾选的量级才会检索</b>，没勾的完全不找；每档可以单独设条数（默认 20）。
          这里的<b>勾选和条数只对本次检索生效</b>；基准价与倍数点「保存档位」后会写进本机
          <span className="font-mono"> pricing.json</span>，下一次检索立即生效。
        </p>

        <div className="flex flex-wrap items-center gap-3 text-xs">
          {config && (
            <span className={config.configured
              ? "flex items-center gap-1.5 text-success"
              : "flex items-center gap-1.5 text-warning"}>
              {config.configured
                ? <CheckCircle2 className="h-3.5 w-3.5" />
                : <AlertCircle className="h-3.5 w-3.5" />}
              {config.configured ? "已配置" : "未配置（预算把关关闭）"}
            </span>
          )}
          <label className="flex items-center gap-2 text-text-secondary">
            倍数
            <input
              type="number" min={0.1} max={10} step={0.05}
              className="h-8 w-24 rounded-control border border-border bg-surface px-2 text-sm text-text-primary disabled:opacity-50"
              value={multiplier}
              disabled={disabled}
              onChange={(event) => setMultiplier(event.target.value)}
            />
          </label>
          {config?.source && (
            <span className="text-[11px] text-text-tertiary">来源：{config.source}</span>
          )}
        </div>

        <div className="overflow-x-auto rounded-control border border-border">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="bg-surface-secondary text-left text-xs text-text-secondary">
                <th className="w-14 whitespace-nowrap px-3 py-2 font-medium">检索</th>
                <th className="whitespace-nowrap px-3 py-2 font-medium">档位</th>
                <th className="whitespace-nowrap px-3 py-2 font-medium">粉丝区间</th>
                <th className="whitespace-nowrap px-3 py-2 font-medium">图文基准价（元）</th>
                <th className="whitespace-nowrap px-3 py-2 font-medium">视频基准价（元）</th>
                <th className="whitespace-nowrap px-3 py-2 font-medium">图文上限</th>
                <th className="whitespace-nowrap px-3 py-2 font-medium">视频上限</th>
                <th className="whitespace-nowrap px-3 py-2 font-medium">取多少条</th>
              </tr>
            </thead>
            <tbody>
              {pricingTiers.map((tier, index) => {
                const key = tierKey(tier);
                const setting = tierSettings.find((item) => item.key === key)
                  ?? { key, enabled: true, count: DEFAULT_COUNT };
                return (
                  <tr key={key}
                      className={["border-t border-border", setting.enabled ? "" : "opacity-60"]
                        .join(" ")}>
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-primary"
                        checked={setting.enabled}
                        disabled={disabled}
                        aria-label={`检索 ${key}`}
                        onChange={(event) => setSetting(key, { enabled: event.target.checked })}
                      />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-text-primary">
                      {tier.label}<span className="text-text-tertiary">（{tier.bracket}）</span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-xs text-text-secondary">
                      {tier.min / 10_000}w–{tier.max / 10_000}w
                    </td>
                    <td className="px-3 py-2">
                      <input
                        type="number" min={0} step={100}
                        className="h-8 w-32 rounded-control border border-border bg-surface px-2 text-right text-sm tabular-nums text-text-primary disabled:opacity-50"
                        value={tier.image}
                        disabled={disabled}
                        onChange={(event) => setPrice(index, "image", event.target.value)}
                      />
                    </td>
                    <td className="px-3 py-2">
                      <input
                        type="number" min={0} step={100}
                        className="h-8 w-32 rounded-control border border-border bg-surface px-2 text-right text-sm tabular-nums text-text-primary disabled:opacity-50"
                        value={tier.video}
                        disabled={disabled}
                        onChange={(event) => setPrice(index, "video", event.target.value)}
                      />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-text-secondary">
                      {money(cap(tier.image))}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-text-secondary">
                      {money(cap(tier.video))}
                    </td>
                    <td className="px-3 py-2">
                      <input
                        type="number" min={1} max={500}
                        className="h-8 w-28 rounded-control border border-border bg-surface px-2 text-right text-sm tabular-nums text-text-primary disabled:opacity-50"
                        value={setting.count}
                        disabled={disabled || !setting.enabled}
                        aria-label={`${key} 取多少条`}
                        onChange={(event) => setSetting(key, {
                          count: Math.max(1, Math.min(500, Number(event.target.value) || 1)),
                        })}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button className="h-9 px-4" onClick={() => void save()}
                  disabled={saving || disabled || pricingTiers.length === 0}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            保存档位
          </Button>
          <Button variant="secondary" className="h-9 px-4"
                  onClick={() => void restoreDefault()}
                  disabled={disabled || config?.default_available === false}>
            <RotateCcw className="h-4 w-4" /> 恢复开发默认
          </Button>
          {message && <span className="text-xs text-text-secondary">{message}</span>}
        </div>

        <p className="text-xs text-text-tertiary">
          本次计划采集 <b className="text-text-secondary">{plannedCount}</b> 条
          （{enabledSettings.length} 个量级）。
        </p>
        {tierSettings.length > 0 && enabledSettings.length === 0 && (
          <p className="rounded-control border border-warning/40 bg-warning/5 px-3 py-2 text-xs text-text-secondary">
            <b className="text-warning">一个量级都没勾。</b>
            这样没有可检索的人 —— 至少勾选一个再开始。
          </p>
        )}
      </CardContent>
    </Card>
  );
}
