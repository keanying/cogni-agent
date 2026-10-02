import React, { useEffect, useState } from 'react';
import { AlertTriangle, Loader2 } from 'lucide-react';
import { Button } from './button';
import { Modal } from './modal';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  loading?: boolean;
  /** 显示「强制删除(自动解除绑定)」复选框；勾选后 onConfirm(force=true)。 */
  forceEnabled?: boolean;
  onConfirm: (force?: boolean) => void;
  onClose: () => void;
}

/** 通用删除确认弹窗：遮罩/Esc 关闭，确认按钮 loading 态防重复提交；
 *  可选强制删除开关（删除保护加固：默认 Restrict，勾选强制时自动解除绑定后归档）。 */
export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  open,
  title,
  description,
  confirmText = '确认删除',
  cancelText = '取消',
  loading = false,
  forceEnabled = false,
  onConfirm,
  onClose,
}) => {
  const [force, setForce] = useState(false);
  useEffect(() => {
    if (open) setForce(false);
  }, [open]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      width="max-w-md"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={loading} className="rounded-xl h-10">
            {cancelText}
          </Button>
          <Button variant="destructive" onClick={() => onConfirm(force)} disabled={loading} className="rounded-xl h-10 gap-2">
            {loading && <Loader2 size={14} className="animate-spin" />}
            {confirmText}
          </Button>
        </>
      }
    >
      <div className="flex items-start gap-3">
        <div className="h-10 w-10 shrink-0 rounded-xl bg-rose-50 flex items-center justify-center text-rose-500">
          <AlertTriangle size={18} />
        </div>
        <div className="text-sm leading-relaxed text-slate-600 flex-1">
          {description}
          {forceEnabled && (
            <label className="mt-4 flex items-start gap-2 text-sm text-slate-700 cursor-pointer select-none rounded-lg bg-amber-50 border border-amber-200 p-2.5">
              <input
                type="checkbox"
                checked={force}
                onChange={(e) => setForce(e.target.checked)}
                className="mt-0.5 accent-rose-500"
              />
              <span className="leading-snug">
                强制删除
                <span className="block text-xs text-slate-500 mt-0.5">
                  有绑定/被引用时将自动解除绑定关系（不删除绑定方），删除后归档不可恢复
                </span>
              </span>
            </label>
          )}
        </div>
      </div>
    </Modal>
  );
};

export default ConfirmDialog;
