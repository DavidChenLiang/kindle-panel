-- Kindle Info Panel - KOReader 自动刷新插件 v8
-- 安装: kindle_panel.koplugin/ 整个文件夹放到 /mnt/us/koreader/plugins/
-- 日志: /mnt/us/koreader/panel_debug.log
--
-- 历史:
--   v5/v6 用 ReaderUI:showReader() 打开图片 -> 每开一次就新建 ReaderUI
--   -> 新 UI 会重新实例化本插件 -> init() 又 showReader -> 无限自激(不停闪)
--   v7 改为在 FileManager 之上叠一层全屏 ImageWidget, 不走 ReaderUI
--   v8 在 v7 基础上做"一次成"加固:
--     1. 所有 UI 模块改为用时 pcall 懒加载 — 任何一个模块路径不对也不会让插件加载失败
--     2. 任何失败直接把原因显示在屏幕上(Notification/InfoMessage) — 不用插 USB 就能看到
--     3. 先上屏、后联网 — 网络慢/断也不影响"面板出现"这件事
--     4. 显示前先校验本地 PNG 完整性, 避免把半截文件喂给渲染器
--     5. 菜单里加"信息面板: 立即显示", 关掉后不重启也能叫回来

local PANEL_PATH = "/mnt/us/documents/panel.png"
local LOG_PATH = "/mnt/us/koreader/panel_debug.log"
local TMP_PATH = "/mnt/us/documents/panel.png.tmp"
local HEARTBEAT_PATH = "/mnt/us/koreader/panel_alive.txt"

-- 日志函数必须最先定义: 连骨架都拿不到时也要留下痕迹
local function log(msg)
    local line = os.date("%H:%M:%S ") .. tostring(msg)
    print("[kindle_panel] " .. line)
    local ok, f = pcall(io.open, LOG_PATH, "a")
    if ok and f then
        pcall(function()
            f:write(line .. "\n")
            f:close()
        end)
    end
end

local ok_wc, WidgetContainer = pcall(require, "ui/widget/container/widgetcontainer")
local ok_um, UIManager = pcall(require, "ui/uimanager")
local ok_dev, Device = pcall(require, "device")

-- 这三样是骨架, 拿不到就真的什么也做不了
if not (ok_wc and ok_um and ok_dev) then
    log("FATAL skeleton missing: wc=" .. tostring(ok_wc) ..
        " um=" .. tostring(ok_um) .. " dev=" .. tostring(ok_dev))
    return {}
end

local Screen = Device.screen

local ok_lfs, lfs = pcall(require, "libs/libkoreader-lfs")

local START_DELAY = 6       -- 启动后先上屏
local FIRST_PULL_DELAY = 12 -- 上屏之后才联网(确保画面先出来)
local CHECK_INTERVAL = 300  -- 之后每 5 分钟检查一次
local MIN_PNG_BYTES = 20000 -- 小于这个体积视为无效
local HTTP_TIMEOUT = 15     -- 单次请求上限(秒), 关键: 不能让 UI 线程被 socket 拖死

local PULL_URL = "https://davidchenliang.github.io/kindle-panel/panel.png"

local KindlePanel = WidgetContainer:extend{
    name = "kindle_panel",
    fullname = "Kindle Info Panel",
    description = "Full-screen auto-refreshing info panel",
    is_doc_only = false,
}

-- 模块级共享状态(同一 KOReader 进程内所有实例共用)
local panel_widget = nil
local panel_image = nil
local tick_running = false
local user_dismissed = false
local last_mtime = 0
local err_notified = false

-- 在屏幕上弹出提示; 插件出问题时用户不插 USB 也能看到原因
local function notify(msg)
    local ok_n, Notification = pcall(require, "ui/widget/notification")
    if ok_n and Notification then
        local ok = pcall(function()
            UIManager:show(Notification:new{ text = tostring(msg), timeout = 12 })
        end)
        if ok then return end
    end
    local ok_i, InfoMessage = pcall(require, "ui/widget/infomessage")
    if ok_i and InfoMessage then
        pcall(function()
            UIManager:show(InfoMessage:new{ text = tostring(msg), timeout = 12 })
        end)
    end
end

local function report_error(where, err)
    log(where .. " ERROR: " .. tostring(err))
    if not err_notified then
        err_notified = true
        notify("信息面板出错: " .. tostring(err))
    end
end

local function file_mtime(path)
    if not ok_lfs then return 0 end
    local ok, a = pcall(lfs.attributes, path)
    if ok and a then return a.modification or 0 end
    return 0
end

local function files_equal(a, b)
    local fa = io.open(a, "rb")
    if not fa then return false end
    local fb = io.open(b, "rb")
    if not fb then
        pcall(function() fa:close() end)
        return false
    end
    local same = true
    while true do
        local ba = fa:read(65536)
        local bb = fb:read(65536)
        if ba ~= bb then same = false break end
        if not ba then break end
    end
    pcall(function() fa:close() end)
    pcall(function() fb:close() end)
    return same
end

-- 必须是完整 PNG: 8 字节签名 + 体积够大
local function valid_png(path)
    local f = io.open(path, "rb")
    if not f then return false, "cannot open" end
    local head = f:read(8)
    local size = f:seek("end")
    f:close()
    if not head or #head < 8 then return false, "too short" end
    if head ~= "\137PNG\r\n\26\n" then return false, "not png" end
    if not size or size < MIN_PNG_BYTES then return false, "size=" .. tostring(size) end
    return true
end

local function fetch_to(url, path)
    local ok_ltn, ltn12 = pcall(require, "ltn12")
    if not ok_ltn then return false, "no ltn12" end
    local mod
    if url:match("^https://") then
        local ok, https = pcall(require, "ssl.https")
        if not ok then return false, "no ssl.https" end
        mod = https
    else
        local ok, http = pcall(require, "socket.http")
        if not ok then return false, "no socket.http" end
        mod = http
    end
    local fh = io.open(path, "wb")
    if not fh then return false, "open tmp failed" end
    -- timeout 必须给: 这个请求跑在 UI 主线程, 不设上限会把整个 KOReader 冻住
    local ok, r1, code = pcall(mod.request, {
        url = url,
        sink = ltn12.sink.file(fh),
        timeout = HTTP_TIMEOUT,
    })
    if not ok then
        pcall(function() fh:close() end)
        return false, "req_error=" .. tostring(r1)
    end
    pcall(function() fh:close() end)
    if code == 200 then return true end
    return false, "http=" .. tostring(code) .. " detail=" .. tostring(r1)
end

local function remove_panel()
    if panel_widget then
        local w = panel_widget
        panel_widget = nil
        panel_image = nil
        pcall(function() UIManager:close(w) end)
        log("remove_panel: closed")
    end
end

-- 全屏显示 panel.png; 不走 ReaderUI, 不会触发插件重入
local function show_panel()
    remove_panel()

    local fh = io.open(PANEL_PATH, "rb")
    if not fh then
        log("show_panel: MISSING " .. PANEL_PATH)
        -- 图上不来时把原因摆到屏幕上, 而不是让用户面对一个空屏
        notify("面板图还没到位: documents/panel.png 不存在")
        return false
    end
    fh:close()

    local vok, verr = valid_png(PANEL_PATH)
    if not vok then
        report_error("show_panel", "local png invalid (" .. tostring(verr) .. ")")
        return false
    end

    local sw, sh = Screen:getWidth(), Screen:getHeight()
    local ok, err = pcall(function()
        -- 懒加载: 任何一个模块缺失都只影响"显示", 不会让插件加载失败
        local ImageWidget = require("ui/widget/imagewidget")
        local InputContainer = require("ui/widget/container/inputcontainer")
        local Geom = require("ui/geometry")
        local GestureRange = require("ui/gesturerange")

        local img = ImageWidget:new{
            file = PANEL_PATH,
            file_do_cache = false, -- 关键: 不用缓存, 文件换了立刻读到新图
            scale_factor = 0,      -- 等比缩放到下面给的宽高
            width = sw,
            height = sh,
        }
        local full = Geom:new{ x = 0, y = 0, w = sw, h = sh }
        local w = InputContainer:new{
            [1] = img,
            dimen = full,
            ges_events = {
                Tap = {
                    GestureRange:new{ ges = "tap", range = full },
                },
            },
        }
        function w:onTap()
            log("onTap -> dismiss panel")
            user_dismissed = true
            panel_widget = nil
            panel_image = nil
            UIManager:close(self)
            return true
        end
        panel_widget = w
        panel_image = img
        UIManager:show(w, "full")
    end)
    if not ok then
        -- 退路: 上面那条路不行就用 KOReader 官方看图组件, 多一条命
        panel_widget = nil
        panel_image = nil
        log("show_panel: primary failed (" .. tostring(err) .. "), try ImageViewer")
        local ok2, err2 = pcall(function()
            local ImageViewer = require("ui/widget/imageviewer")
            local v = ImageViewer:new{
                file = PANEL_PATH,
                with_title_bar = false,
                fullscreen = true,
            }
            panel_widget = v
            panel_image = nil
            UIManager:show(v, "full")
        end)
        if not ok2 then
            panel_widget = nil
            panel_image = nil
            report_error("show_panel",
                tostring(err) .. " | fallback: " .. tostring(err2))
            return false
        end
        log("show_panel: ok via ImageViewer fallback")
        return true
    end
    log("show_panel: ok " .. sw .. "x" .. sh)
    return true
end

-- 内容变了: 面板在屏上就原地换图, 否则重新挂上去
local function update_panel()
    if panel_widget and panel_image and UIManager:isWidgetShown(panel_widget) then
        local ok, err = pcall(function()
            -- free() 把 _bb 置空; 下次 paintTo 会因 file_do_cache=false 重新读盘
            panel_image:free()
            panel_image.file = PANEL_PATH -- 保险: 万一 free 清了 file
            UIManager:setDirty(panel_widget, "full")
        end)
        if ok then
            log("update_panel: in-place refresh")
            return true
        end
        log("update_panel: in-place failed " .. tostring(err))
    end
    return show_panel()
end

local function do_pull()
    if PULL_URL == "" then return false end
    local ok, err = fetch_to(PULL_URL, TMP_PATH)
    if not ok then
        log("pull: FAILED " .. tostring(err))
        pcall(os.remove, TMP_PATH)
        return false
    end
    local vok, verr = valid_png(TMP_PATH)
    if not vok then
        log("pull: BAD FILE " .. tostring(verr))
        pcall(os.remove, TMP_PATH)
        return false
    end
    if files_equal(TMP_PATH, PANEL_PATH) then
        pcall(os.remove, TMP_PATH)
        log("pull: unchanged")
        return false
    end
    if not os.rename(TMP_PATH, PANEL_PATH) then
        log("pull: rename FAILED")
        pcall(os.remove, TMP_PATH)
        return false
    end
    log("pull: CHANGED -> replaced")
    return true
end

local function refresh_once()
    local ok, err = pcall(function()
        local changed
        if PULL_URL ~= "" then
            changed = do_pull()
        else
            changed = file_mtime(PANEL_PATH) > last_mtime
        end
        last_mtime = file_mtime(PANEL_PATH)

        if user_dismissed then
            -- 用户自己关掉的, 只更新文件不弹回来
        elseif changed then
            update_panel()
        elseif panel_widget and not UIManager:isWidgetShown(panel_widget) then
            log("refresh: panel lost -> re-show")
            panel_widget = nil
            panel_image = nil
            show_panel()
        end
    end)
    if not ok then report_error("refresh", err) end
end

local function tick()
    refresh_once()
    UIManager:scheduleIn(CHECK_INTERVAL, tick)
end

function KindlePanel:init()
    local ui_name = (self.ui and self.ui.name) or "?"
    -- 只在文件管理器工作; 打开书时那个 UI 也会实例化本插件, 一律不介入
    if ui_name:find("^reader") or (self.ui and self.ui.document) then
        log("skip init in " .. ui_name)
        return
    end

    log("INIT v8 loaded, ui=" .. ui_name ..
        " panel=" .. tostring(file_mtime(PANEL_PATH) ~= 0) ..
        " net=" .. tostring(PULL_URL ~= ""))

    if tick_running then
        log("tick already running, not stacking another chain")
        return
    end
    tick_running = true
    last_mtime = file_mtime(PANEL_PATH)

    -- 第一步: 只上屏, 不联网。保证画面先出来。
    UIManager:scheduleIn(START_DELAY, function()
        local ok, err = pcall(function()
            if not user_dismissed then show_panel() end
        end)
        if not ok then report_error("first-show", err) end
    end)

    -- 第二步: 画面出来之后才开始联网, 网络慢也不会拖住第一屏
    UIManager:scheduleIn(FIRST_PULL_DELAY, tick)
end

-- 菜单入口: 关掉面板后不重启也能叫回来
function KindlePanel:addToMainMenu(menu_items)
    menu_items.kindle_panel = {
        text = "信息面板：立即显示",
        callback = function()
            user_dismissed = false
            err_notified = false
            show_panel()
            UIManager:scheduleIn(2, refresh_once)
        end,
    }
end

return KindlePanel
