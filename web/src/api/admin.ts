/**
 * 用户管理（admin）API。
 */
import request from "@/utils/request";

export interface AdminCapabilities {
  is_admin: boolean;
}

export interface AdminRole {
  id: number;
  code: string;
  name: string;
}

export interface AdminUnit {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
}

export interface AdminUser {
  id: number;
  username: string;
  phone?: string | null;
  unit_id: number;
  role_id: number;
  is_active: boolean;
  created_at?: string;
  updated_at?: string;
}

export const getAdminCapabilities = () =>
  request.get<any, AdminCapabilities>("/api/admin/capabilities");

export const listAdminRoles = () =>
  request.get<any, AdminRole[]>("/api/admin/roles");

export const listAdminUnits = () =>
  request.get<any, AdminUnit[]>("/api/admin/units");

export const listAdminUsers = () =>
  request.get<any, AdminUser[]>("/api/admin/users");

export const patchAdminUser = (
  userId: number,
  body: { role_id?: number; unit_id?: number; is_active?: boolean }
) => request.patch<any, { success: boolean; message: string }>(
  `/api/admin/users/${userId}`,
  body
);
