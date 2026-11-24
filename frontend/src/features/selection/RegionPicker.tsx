import { useEffect, useRef, useState } from "react";
import type { AreaNode } from "../../lib/api";

/**
 * 地域选择：**照网页版的多列级联 + 勾选**。
 *
 * 网页版的行为（2026-11-19 实测）：
 * - 四列并排：国家 → 省 → 市 → 区；
 * - 勾一个父级会**级联勾上它下面所有项**；
 * - 取消一个子项，父级变成**部分选中**（不是把「全部」或父级一起取消）；
 * - 每列顶部「全部」是**本列全选/全不选**：
 *   全选时再点取消本列，未全选时点它选中本列全部。
 *
 * `selected` 存的是**完整路径**（如 `中国 安徽 合肥 蜀山区`），只保留最上层
 * 被勾选的节点；请求体和本地把关都由后端按这个路径处理。
 */
export function RegionPicker({
  areas,
  selected,
  onChange,
  disabled = false,
}: {
  areas: AreaNode[];
  selected: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const [country, setCountry] = useState<AreaNode | null>(null);
  const [province, setProvince] = useState<AreaNode | null>(null);
  const [city, setCity] = useState<AreaNode | null>(null);
  const chosen = new Set(selected);

  const pathOf = (parentPath: string, node: AreaNode) =>
    parentPath ? `${parentPath} ${node.value}` : node.value;

  const normalize = (paths: string[]): string[] => {
    const unique = Array.from(new Set(paths.filter(Boolean)));
    return unique.filter((path) => {
      const parts = path.split(" ");
      for (let i = 1; i < parts.length; i += 1) {
        if (unique.includes(parts.slice(0, i).join(" "))) return false;
      }
      return true;
    });
  };

  const isCovered = (path: string): boolean => {
    const parts = path.split(" ");
    for (let i = 1; i <= parts.length; i += 1) {
      if (chosen.has(parts.slice(0, i).join(" "))) return true;
    }
    return false;
  };

  const hasSelectedDescendant = (path: string): boolean =>
    selected.some((item) => item !== path && item.startsWith(`${path} `));

  const findNode = (nodes: AreaNode[], parts: string[]): AreaNode | null => {
    if (parts.length === 0) return null;
    const node = nodes.find((item) => item.value === parts[0]);
    if (!node) return null;
    if (parts.length === 1) return node;
    return findNode(node.children ?? [], parts.slice(1));
  };

  const toggleNode = (path: string) => {
    if (disabled) return;
    const parts = path.split(" ");

    if (!isCovered(path)) {
      // 未选中 → 选中这个节点；它下面的已选子项被父级覆盖，去掉。
      onChange(normalize([
        ...selected.filter((item) => item !== path && !item.startsWith(`${path} `)),
        path,
      ]));
      return;
    }

    if (chosen.has(path)) {
      // 自己就是被选中的节点 → 取消它和它下面所有项。
      onChange(selected.filter((item) => item !== path && !item.startsWith(`${path} `)));
      return;
    }

    // 被某个祖先覆盖 → 取消这个分支，但保留祖先下面其它分支。
    let ancestorPath = "";
    for (let i = parts.length - 1; i >= 1; i -= 1) {
      const candidate = parts.slice(0, i).join(" ");
      if (chosen.has(candidate)) {
        ancestorPath = candidate;
        break;
      }
    }
    if (!ancestorPath) return;
    const ancestorParts = ancestorPath.split(" ");
    const ancestorNode = findNode(areas, ancestorParts);
    const branchValue = parts[ancestorParts.length];
    const siblings = (ancestorNode?.children ?? [])
      .filter((child) => child.value !== branchValue)
      .map((child) => `${ancestorPath} ${child.value}`);
    onChange(normalize([
      ...selected.filter((item) => item !== ancestorPath
        && !item.startsWith(`${ancestorPath} `)),
      ...siblings,
    ]));
  };

  const columnState = (items: AreaNode[], parentPath: string) => {
    const covered = items.map((node) => isCovered(pathOf(parentPath, node)));
    return {
      all: items.length > 0 && covered.every(Boolean),
      // 只要本列下面**任意一层**有选中，就算部分选中（横线）。
      // 只数「直接子项」会把「只选了区县」误判成全空。
      some: covered.some(Boolean)
        || selected.some((path) => (parentPath ? path.startsWith(`${parentPath} `) : true)),
    };
  };

  const toggleAll = (items: AreaNode[], parentPath: string) => {
    if (disabled || items.length === 0) return;
    const { all } = columnState(items, parentPath);
    if (!parentPath) {
      // 第一列（国家）：全部 = 选中所有国家；再点 = 清空。
      onChange(all ? [] : normalize(areas.map((node) => node.value)));
      return;
    }
    if (all) {
      onChange(selected.filter((item) => item !== parentPath
        && !item.startsWith(`${parentPath} `)));
      return;
    }
    onChange(normalize([
      ...selected.filter((item) => item !== parentPath
        && !item.startsWith(`${parentPath} `)),
      parentPath,
    ]));
  };

  const expand = (level: 0 | 1 | 2, node: AreaNode) => {
    if (level === 0) {
      setCountry(node);
      setProvince(null);
      setCity(null);
    } else if (level === 1) {
      setProvince(node);
      setCity(null);
    } else {
      setCity(node);
    }
  };

  const column = (
    items: AreaNode[],
    parentPath: string,
    level: 0 | 1 | 2 | 3,
    active: AreaNode | null,
    onExpand: (node: AreaNode) => void,
  ) => {
    const state = columnState(items, parentPath);
    return (
      <div
        // 换父级时强制重挂载：否则滚动条会停在上一个列表的底部，
        // 新列表看起来就像「少了前面几项」（用户踩到过）。
        key={parentPath || "__root__"}
        className={[
          "h-[360px] shrink-0 overflow-y-scroll border-r border-border px-1 py-1 last:border-r-0",
          level === 3 ? "w-[230px]" : "w-[160px]",
        ].join(" ")}
      >
        <div className="flex h-9 items-center gap-2 rounded-control px-2 text-sm text-text-secondary hover:bg-surface-hover">
          <TriStateCheckbox
            checked={state.all}
            indeterminate={state.some && !state.all}
            disabled={disabled}
            ariaLabel="本列全部"
            onChange={() => toggleAll(items, parentPath)}
          />
          <button
            type="button"
            className="min-w-0 flex-1 truncate text-left"
            disabled={disabled}
            onClick={() => toggleAll(items, parentPath)}
          >
            全部
          </button>
        </div>
        {items.map((node) => {
          const path = pathOf(parentPath, node);
          const children = node.children ?? [];
          const hasChildren = children.length > 0;
          const checked = isCovered(path);
          const isActive = active?.value === node.value;
          return (
            <div
              key={path}
              className={[
                "flex h-9 items-center gap-2 rounded-control px-2 text-sm hover:bg-surface-hover",
                isActive ? "bg-surface-secondary" : "",
              ].join(" ")}
            >
              <TriStateCheckbox
                checked={checked}
                indeterminate={!checked && hasSelectedDescendant(path)}
                disabled={disabled}
                ariaLabel={`地域 ${node.label}`}
                onChange={() => toggleNode(path)}
              />
              <button
                type="button"
                className="min-w-0 flex-1 truncate text-left text-text-primary disabled:opacity-50"
                disabled={disabled}
                title={node.label}
                onClick={() => {
                  if (hasChildren && level < 3) onExpand(node);
                  toggleNode(path);
                }}
              >
                {node.label}
              </button>
              {hasChildren && level < 3 && (
                <button
                  type="button"
                  className="shrink-0 px-1 text-base text-text-tertiary hover:text-text-primary disabled:opacity-50"
                  disabled={disabled}
                  aria-label={`展开 ${node.label}`}
                  title="只展开，不改变勾选"
                  onClick={() => onExpand(node)}
                >
                  ›
                </button>
              )}
            </div>
          );
        })}
      </div>
    );
  };

  const countryPath = country?.value ?? "";
  const provincePath = country && province ? `${country.value} ${province.value}` : "";
  const cityPath = country && province && city
    ? `${country.value} ${province.value} ${city.value}`
    : "";

  return (
    <div className="flex overflow-x-auto bg-surface">
      {column(areas, "", 0, country, (node) => expand(0, node))}
      {country && column(
        country.children ?? [], countryPath, 1, province, (node) => expand(1, node),
      )}
      {province && column(
        province.children ?? [], provincePath, 2, city, (node) => expand(2, node),
      )}
      {city && column(city.children ?? [], cityPath, 3, null, () => undefined)}
    </div>
  );
}

/** 三态复选框：`indeterminate` 不是 HTML 属性，只能通过 DOM 属性设置。 */
function TriStateCheckbox({
  checked,
  indeterminate,
  disabled,
  ariaLabel,
  onChange,
}: {
  checked: boolean;
  indeterminate: boolean;
  disabled: boolean;
  ariaLabel: string;
  onChange: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input
      ref={ref}
      type="checkbox"
      className="h-4 w-4 shrink-0 accent-primary"
      checked={checked}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={onChange}
    />
  );
}
