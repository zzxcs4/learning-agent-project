<script setup lang="ts">
import type { LearningQuizQuestion } from '@/types/chat'

/**
 * 答题表单（受控组件）：题目 + 答案对象进来，改答案通过 `update:modelValue` 出去。
 *
 * 两个入口共用这一套渲染，避免同一份"题型 → 控件"的映射写两遍：
 * - 聊天里的答题弹窗（`LearningQuizDialog`，整套题作答）；
 * - 练习记录里的"补答"（`PracticeHistory`，只补未作答的题）。
 *
 * 它**不做校验、不提交**：两边的必答口径不同（整套题要求全答，补答只要求本次填的那些非空）。
 * 滚动区域也留给调用方（弹窗 720px 与全屏下的可视高度不一样）。
 */
const props = defineProps<{
  questions: LearningQuizQuestion[]
  modelValue: Record<string, unknown>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, unknown>]
}>()

/** 每题标题：序号 + 题干 + 可选分值。 */
const questionTitle = (question: LearningQuizQuestion, index: number) => {
  const points = typeof question.points === 'number' ? `（${question.points}分）` : ''
  return `${index + 1}. ${question.stem}${points}`
}

/** 改单题答案：整体换对象而不是就地改 props。 */
const setAnswer = (questionId: string, value: unknown) => {
  emit('update:modelValue', { ...props.modelValue, [questionId]: value })
}

// 按题型取答案：Element Plus 的组控件要求特定入参类型，这里把 unknown 收敛掉
const radioAnswer = (questionId: string) =>
  props.modelValue[questionId] as string | number | boolean | undefined
const checkAnswer = (questionId: string) => {
  const value = props.modelValue[questionId]
  return Array.isArray(value) ? (value as string[]) : []
}
const textAnswer = (questionId: string) => String(props.modelValue[questionId] ?? '')
</script>

<template>
  <el-form label-position="top" class="quiz-answer-form">
    <el-form-item
      v-for="(question, index) in questions"
      :key="question.id"
      :label="questionTitle(question, index)">
      <!-- 单选题 -->
      <el-radio-group
        v-if="question.type === 'radio'"
        :model-value="radioAnswer(question.id)"
        @update:model-value="setAnswer(question.id, $event)">
        <el-radio v-for="option in question.options" :key="option.value" :value="option.value">
          {{ option.label }}
        </el-radio>
      </el-radio-group>

      <!-- 多选题 -->
      <el-checkbox-group
        v-else-if="question.type === 'checkbox'"
        :model-value="checkAnswer(question.id)"
        @update:model-value="setAnswer(question.id, $event)">
        <el-checkbox v-for="option in question.options" :key="option.value" :value="option.value">
          {{ option.label }}
        </el-checkbox>
      </el-checkbox-group>

      <!-- 默认按主观题处理 -->
      <el-input
        v-else
        :model-value="textAnswer(question.id)"
        type="textarea"
        :rows="4"
        placeholder="请输入你的答案"
        @update:model-value="setAnswer(question.id, $event)" />
    </el-form-item>
  </el-form>
</template>

<style scoped lang="scss">
.quiz-answer-form {
  padding-right: 8px;
}

:deep(.el-radio),
:deep(.el-checkbox) {
  display: flex;
  height: auto;
  margin-bottom: 8px;
  white-space: normal;
}
</style>
