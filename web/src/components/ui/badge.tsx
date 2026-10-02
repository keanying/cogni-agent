
import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "../../lib/utils"

const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-bold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 uppercase tracking-wider",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground hover:bg-primary/80",
        secondary:
          "border-transparent bg-slate-100 text-slate-600 hover:bg-slate-200",
        destructive:
          "border-transparent bg-red-50 text-red-600 border-red-100 hover:bg-red-100",
        outline: "text-slate-500 border-slate-200 bg-white",
        success: "border-transparent bg-emerald-50 text-emerald-600 border-emerald-100",
        warning: "border-transparent bg-amber-50 text-amber-600 border-amber-100",
        info: "border-transparent bg-blue-50 text-blue-600 border-blue-100",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  )
}

export { Badge, badgeVariants }
