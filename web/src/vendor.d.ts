/** Vite 的 `?url` 导入:构建时把文件拷进产物(带哈希名),返回按 base 解析好的地址。 */
declare module '*?url' {
  const src: string;
  export default src;
}
