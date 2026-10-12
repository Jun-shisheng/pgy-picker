import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { AppShell } from "./components/layout/AppShell";
import { AccountsPage } from "./features/accounts/AccountsPage";
import { SelectionPage } from "./features/selection/SelectionPage";
import { SettingsPage } from "./features/settings/SettingsPage";

const rootRoute = createRootRoute({ component: AppShell });

// 只登记真正实现的页面：选号 / 账户 / 设置。
const selectionRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: SelectionPage,
});

const accountsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/accounts",
  component: AccountsPage,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

const routeTree = rootRoute.addChildren([selectionRoute, accountsRoute, settingsRoute]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
