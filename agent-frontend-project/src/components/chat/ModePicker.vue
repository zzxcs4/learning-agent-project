<script setup lang="ts">
/**
 * 模式选择器（第 26 轮 UI 改版）：把原来平铺在输入框底部的几个模式按钮
 * 收成一个「当前模式 ▾」按钮，点开才是候选面板（形态对齐用户给的参考图）。
 *
 * 为什么改成收起来：
 * 1. 平铺时每接一个新智能体就多一个按钮，工具栏会被撑满，还会把附件/知识库挤到后面；
 * 2. 参考图里每个模式都带一句说明（「灵活对话，可使用任意工具」），平铺按钮塞不下这句话，
 *    于是用户只能靠名字猜「出题测评」和「聊天」到底差在哪。
 *
 * 这里**只负责画和选**，模式清单由父级（StudyRoom）给：新增/改名/下线某个模式
 * 只改父级一处，不需要回来改这个组件。
 */
import { ChatDotRound, Check, EditPen, MagicStick } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'
import type { AgentOption } from '@/types/chat'

/** 一个模式在面板里的画法：图标 + 名字 + 一句说明 */
export type ModeCard = AgentOption & {
    /** 一句人话说明这个模式做什么（参考图里的第二行小字） */
    hint: string
    /** 图标名：面板里按名取组件，父级不必为了给图标而 import 组件进来 */
    icon: 'chat' | 'quiz' | 'card'
}

const props = defineProps<{
    /** 候选模式（第一项是默认的「聊天」） */
    modes: ModeCard[]
    /** 当前模式 value；undefined = 默认聊天模式 */
    modelValue?: string
    disabled?: boolean
}>()

const emit = defineEmits<{
    'update:modelValue': [value: string | undefined]
}>()

const ICONS = {
    chat: ChatDotRound,
    quiz: EditPen,
    card: MagicStick,
} as const

const visible = ref(false)

/** 当前选中的模式：value 为空时回落到第一项（默认聊天） */
const current = computed(
    () => props.modes.find((mode) => mode.value === props.modelValue) ?? props.modes[0],
)

/** 面板里靠它打勾 / 标 aria-selected */
const isSelected = (mode: ModeCard) => (props.modelValue ?? '') === mode.value

/** 选中一个模式：默认聊天模式要把值写回 undefined，后端才走「无 agent」的常规链路 */
const pick = (mode: ModeCard) => {
    emit('update:modelValue', mode.value || undefined)
    visible.value = false
}
</script>

<template>
    <el-popover
        v-model:visible="visible"
        trigger="click"
        placement="top-start"
        :width="330"
        :disabled="disabled"
        popper-class="mode-picker__popper">
        <template #reference>
            <button
                type="button"
                class="mode-trigger"
                :disabled="disabled"
                aria-haspopup="listbox"
                :aria-expanded="visible">
                <el-icon class="mode-trigger__icon">
                    <component :is="ICONS[current?.icon ?? 'chat']" />
                </el-icon>
                <span class="mode-trigger__label">{{ current?.label ?? '聊天' }}</span>
                <span
                    class="mode-trigger__caret"
                    :class="{ 'mode-trigger__caret--open': visible }" />
            </button>
        </template>

        <div class="mode-panel" role="listbox">
            <button
                v-for="mode in modes"
                :key="mode.value || '__default__'"
                type="button"
                class="mode-panel__item"
                :class="{ 'mode-panel__item--active': isSelected(mode) }"
                role="option"
                :aria-selected="isSelected(mode)"
                @click="pick(mode)">
                <el-icon class="mode-panel__icon">
                    <component :is="ICONS[mode.icon]" />
                </el-icon>
                <span class="mode-panel__text">
                    <span class="mode-panel__name">{{ mode.label }}</span>
                    <span class="mode-panel__hint">{{ mode.hint }}</span>
                </span>
                <el-icon v-if="isSelected(mode)" class="mode-panel__check"><Check /></el-icon>
            </button>
        </div>
    </el-popover>
</template>

<style scoped lang="scss">
/* ---- 收起态的触发按钮：当前模式 + 下拉箭头 ---- */
.mode-trigger {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 32px;
    padding: 0 10px;
    border: 1px solid transparent;
    border-radius: 8px;
    background: transparent;
    color: #4b5563;
    font-size: 14px;
    cursor: pointer;
    transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;

    &:hover:not(:disabled) {
        border-color: #e2e6ee;
        background: #f4f6fa;
    }

    &:disabled {
        cursor: not-allowed;
        opacity: 0.6;
    }
}

.mode-trigger__icon {
    font-size: 17px;
    color: #6b7280;
}

.mode-trigger__label {
    white-space: nowrap;
    font-weight: 500;
}

/* 下拉箭头：一个旋转 45° 的方框，展开时翻过来 */
.mode-trigger__caret {
    width: 6px;
    height: 6px;
    margin-left: 2px;
    border-right: 1.5px solid #9aa3b2;
    border-bottom: 1.5px solid #9aa3b2;
    transform: translateY(-2px) rotate(45deg);
    transition: transform 0.16s ease;
}

.mode-trigger__caret--open {
    transform: translateY(1px) rotate(-135deg);
}

/* ---- 展开面板：一行一个模式（图标 + 名字 + 说明 + 勾选） ---- */
.mode-panel {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.mode-panel__item {
    display: flex;
    align-items: center;
    gap: 11px;
    width: 100%;
    padding: 8px 10px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: transparent;
    text-align: left;
    cursor: pointer;

    &:hover {
        background: #f5f7fa;
    }
}

.mode-panel__item--active {
    border-color: #dfe6f5;
    background: #f4f7fe;
}

.mode-panel__icon {
    flex-shrink: 0;
    font-size: 19px;
    color: #5b6473;
}

.mode-panel__text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
}

.mode-panel__name {
    font-size: 14px;
    color: #303133;
}

.mode-panel__hint {
    font-size: 12px;
    line-height: 1.5;
    color: #909399;
}

.mode-panel__check {
    margin-left: auto;
    font-size: 15px;
    color: #4b7bd4;
}
</style>
