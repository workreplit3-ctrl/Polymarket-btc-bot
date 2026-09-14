import {
  getGetBotStatusQueryKey,
  useGetBotStatus,
} from '@workspace/api-client-react';
import { Feather } from '@expo/vector-icons';
import * as Haptics from 'expo-haptics';
import { useCallback, useState } from 'react';
import {
  Alert,
  ActivityIndicator,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useColors } from '@/hooks/useColors';

type FeatherName = keyof typeof Feather.glyphMap;

const statusLabels: Record<string, string> = {
  starting: 'Запускается',
  running: 'Работает',
  stopped: 'Остановлен',
  error: 'Ошибка',
};

function Metric({
  icon,
  label,
  value,
  tone,
}: {
  icon: FeatherName;
  label: string;
  value: string;
  tone?: 'green' | 'amber' | 'muted';
}) {
  const colors = useColors();
  const iconColor =
    tone === 'green'
      ? colors.primary
      : tone === 'amber'
        ? colors.warning
        : colors.mutedForeground;

  return (
    <View style={[styles.metric, { borderColor: colors.border }]}>
      <View style={[styles.metricIcon, { backgroundColor: colors.secondary }]}>
        <Feather name={icon} size={16} color={iconColor} />
      </View>
      <Text style={[styles.metricLabel, { color: colors.mutedForeground }]}>
        {label}
      </Text>
      <Text style={[styles.metricValue, { color: colors.foreground }]}>
        {value}
      </Text>
    </View>
  );
}

export default function HomeScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const statusQuery = useGetBotStatus({
    query: {
      queryKey: getGetBotStatusQueryKey(),
      refetchInterval: 15_000,
    },
  });
  const [controlPending, setControlPending] = useState(false);

  const refresh = useCallback(async () => {
    await Haptics.selectionAsync();
    await statusQuery.refetch();
  }, [statusQuery]);

  const status = statusQuery.data;
  const isRunning = status?.process === 'running';
  const isError = status?.process === 'error';
  const isPaused = status?.paused === true;
  const webTopInset = Platform.OS === 'web' ? 67 : 0;
  const webBottomInset = Platform.OS === 'web' ? 34 : 0;

  const sendControl = useCallback(
    async (action: 'pause' | 'resume') => {
      setControlPending(true);
      try {
        const domain = process.env.EXPO_PUBLIC_DOMAIN;
        const baseUrl = domain ? `https://${domain}` : '';
        const response = await fetch(`${baseUrl}/api/bot/${action}`, { method: 'POST' });
        if (!response.ok) {
          throw new Error(`Control request failed: ${response.status}`);
        }
        await Haptics.notificationAsync(
          action === 'pause'
            ? Haptics.NotificationFeedbackType.Warning
            : Haptics.NotificationFeedbackType.Success,
        );
        await statusQuery.refetch();
      } catch {
        Alert.alert(
          'Не удалось изменить состояние',
          'Сервер не подтвердил действие. Обновите статус и попробуйте ещё раз.',
        );
      } finally {
        setControlPending(false);
      }
    },
    [statusQuery],
  );

  const handleControlPress = useCallback(() => {
    if (isPaused) {
      Alert.alert(
        'Возобновить real-режим?',
        'После возобновления стратегия снова сможет создавать новые ордера.',
        [
          { text: 'Отмена', style: 'cancel' },
          { text: 'Возобновить', onPress: () => void sendControl('resume') },
        ],
      );
      return;
    }
    void sendControl('pause');
  }, [isPaused, sendControl]);

  return (
    <View style={[styles.screen, { backgroundColor: colors.background }]}>
      <ScrollView
        contentContainerStyle={[
          styles.content,
          { paddingTop: insets.top + webTopInset + 18, paddingBottom: insets.bottom + webBottomInset + 30 },
        ]}
        refreshControl={
          <RefreshControl
            refreshing={statusQuery.isFetching}
            onRefresh={refresh}
            tintColor={colors.primary}
          />
        }
        showsVerticalScrollIndicator={false}
      >
        <View style={styles.topline}>
          <View>
            <Text style={[styles.eyebrow, { color: colors.primary }]}>CONTROL CENTER</Text>
            <Text style={[styles.title, { color: colors.foreground }]}>10</Text>
          </View>
          <View style={[styles.liveBadge, { borderColor: colors.border }]}>
            <View
              style={[
                styles.liveDot,
                { backgroundColor: isRunning ? colors.primary : colors.mutedForeground },
              ]}
            />
            <Text style={[styles.liveText, { color: colors.mutedForeground }]}>
              {isRunning ? 'LIVE' : 'OFFLINE'}
            </Text>
          </View>
        </View>

        <View style={[styles.hero, { backgroundColor: colors.card, borderColor: colors.border }]}>
          <View style={styles.heroHeader}>
            <View>
              <Text style={[styles.sectionCaption, { color: colors.mutedForeground }]}>
                TELEGRAM BOT
              </Text>
              <Text style={[styles.heroTitle, { color: colors.foreground }]}>
                {status?.name ?? '10'}
              </Text>
            </View>
            <View style={[styles.modePill, { backgroundColor: colors.accent }]}>
              <Text style={[styles.modeText, { color: colors.accentForeground }]}>
                {status?.mode === 'real' ? 'REAL' : 'PAPER'}
              </Text>
            </View>
          </View>

          <View style={styles.statusRow}>
            <View
              style={[
                styles.statusIcon,
                { backgroundColor: isError ? colors.destructive : colors.secondary },
              ]}
            >
              <Feather
                name={isError ? 'alert-triangle' : isRunning ? 'radio' : 'power'}
                size={22}
                color={isError ? colors.destructiveForeground : colors.primary}
              />
            </View>
            <View style={styles.statusCopy}>
              <Text style={[styles.statusTitle, { color: colors.foreground }]}>
                {status ? statusLabels[status.process] ?? status.process : 'Проверяем связь'}
              </Text>
              <Text style={[styles.statusDescription, { color: colors.mutedForeground }]}>
                {statusQuery.isError
                  ? 'Сервер пока не отвечает. Потяните экран вниз для повторной проверки.'
                  : status?.paused
                    ? 'Стратегия на паузе. Открытые позиции не закрываются автоматически.'
                    : status?.walletConfigured
                      ? 'Кошелёк подключён. Real-режим может отправлять реальные ордера.'
                      : 'Кошелёк не подключён. Бот работает в безопасном режиме paper.'}
              </Text>
            </View>
          </View>
        </View>

        <View style={styles.metricsGrid}>
          <Metric
            icon="shield"
            label="РЕЖИМ"
            value={status?.mode === 'real' ? 'Real' : 'Paper'}
            tone={status?.mode === 'real' ? 'amber' : 'green'}
          />
          <Metric
            icon="lock"
            label="КОШЕЛЁК"
            value={status?.walletConfigured ? 'Connected' : 'Not set'}
            tone={status?.walletConfigured ? 'green' : 'muted'}
          />
        </View>

        <View
          style={[
            styles.controlCard,
            {
              backgroundColor: isPaused ? colors.card : colors.destructive,
              borderColor: colors.border,
            },
          ]}
        >
          <View style={styles.controlCopy}>
            <Text
              style={[
                styles.controlEyebrow,
                { color: isPaused ? colors.warning : colors.destructiveForeground },
              ]}
            >
              {isPaused ? 'СТРАТЕГИЯ НА ПАУЗЕ' : 'REAL CONTROL'}
            </Text>
            <Text
              style={[
                styles.controlTitle,
                { color: isPaused ? colors.foreground : colors.destructiveForeground },
              ]}
            >
              {isPaused ? 'Ордера остановлены' : 'Аварийная пауза'}
            </Text>
            <Text
              style={[
                styles.controlDescription,
                { color: isPaused ? colors.mutedForeground : colors.destructiveForeground },
              ]}
            >
              {isPaused
                ? 'Возобновление снова разрешит новые ордера.'
                : 'Остановить новые ордера без закрытия открытых позиций.'}
            </Text>
          </View>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={isPaused ? 'Возобновить стратегию' : 'Поставить стратегию на паузу'}
            disabled={controlPending || !isRunning}
            onPress={handleControlPress}
            style={({ pressed }) => [
              styles.controlButton,
              {
                backgroundColor: isPaused ? colors.primary : colors.destructiveForeground,
                opacity: pressed || controlPending || !isRunning ? 0.65 : 1,
              },
            ]}
          >
            {controlPending ? (
              <ActivityIndicator color={isPaused ? colors.primaryForeground : colors.destructive} />
            ) : (
              <Feather
                name={isPaused ? 'play' : 'pause'}
                size={17}
                color={isPaused ? colors.primaryForeground : colors.destructive}
              />
            )}
            <Text
              style={[
                styles.controlButtonText,
                { color: isPaused ? colors.primaryForeground : colors.destructive },
              ]}
            >
              {isPaused ? 'Resume' : 'Pause'}
            </Text>
          </Pressable>
        </View>

        <View style={styles.sectionHeading}>
          <Text style={[styles.sectionTitle, { color: colors.foreground }]}>
            Команды
          </Text>
          <Text style={[styles.sectionMeta, { color: colors.mutedForeground }]}>
            Telegram
          </Text>
        </View>

        <View style={[styles.commandCard, { backgroundColor: colors.card, borderColor: colors.border }]}>
          <CommandRow icon="activity" command="/status" description="состояние и позиции" />
          <CommandRow icon="bar-chart-2" command="/markets" description="активные BTC рынки" />
          <CommandRow icon="trending-up" command="/pnl" description="результат за сегодня" />
          <CommandRow icon="pause-circle" command="/pause" description="поставить стратегию на паузу" last />
        </View>

        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Обновить статус"
          onPress={refresh}
          style={({ pressed }) => [
            styles.refreshButton,
            { backgroundColor: colors.primary, opacity: pressed ? 0.78 : 1 },
          ]}
        >
          {statusQuery.isFetching ? (
            <ActivityIndicator color={colors.primaryForeground} />
          ) : (
            <Feather name="refresh-cw" size={18} color={colors.primaryForeground} />
          )}
          <Text style={[styles.refreshText, { color: colors.primaryForeground }]}>
            Обновить статус
          </Text>
        </Pressable>

        <Text style={[styles.footerNote, { color: colors.mutedForeground }]}>
          Ключи и Telegram ID хранятся в защищённых настройках проекта. Они не отображаются в приложении.
        </Text>
      </ScrollView>
    </View>
  );
}

function CommandRow({
  icon,
  command,
  description,
  last = false,
}: {
  icon: FeatherName;
  command: string;
  description: string;
  last?: boolean;
}) {
  const colors = useColors();
  return (
    <View
      style={[
        styles.commandRow,
        !last && { borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.border },
      ]}
    >
      <Feather name={icon} size={17} color={colors.primary} />
      <Text style={[styles.command, { color: colors.foreground }]}>{command}</Text>
      <Text style={[styles.commandDescription, { color: colors.mutedForeground }]}>
        {description}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1 },
  content: { paddingHorizontal: 20, gap: 18 },
  topline: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start' },
  eyebrow: { fontSize: 11, fontWeight: '700', letterSpacing: 2.2 },
  title: { fontSize: 42, fontWeight: '700', letterSpacing: -2 },
  liveBadge: { flexDirection: 'row', alignItems: 'center', gap: 7, borderWidth: 1, borderRadius: 20, paddingHorizontal: 11, paddingVertical: 8, marginTop: 4 },
  liveDot: { width: 7, height: 7, borderRadius: 4 },
  liveText: { fontSize: 10, fontWeight: '700', letterSpacing: 1.2 },
  hero: { borderRadius: 24, borderWidth: 1, padding: 20, gap: 24 },
  heroHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start' },
  sectionCaption: { fontSize: 10, fontWeight: '700', letterSpacing: 1.3, marginBottom: 6 },
  heroTitle: { fontSize: 30, fontWeight: '700', letterSpacing: -1 },
  modePill: { paddingHorizontal: 11, paddingVertical: 7, borderRadius: 12 },
  modeText: { fontSize: 10, fontWeight: '800', letterSpacing: 1.2 },
  statusRow: { flexDirection: 'row', gap: 13, alignItems: 'center' },
  statusIcon: { width: 46, height: 46, borderRadius: 15, alignItems: 'center', justifyContent: 'center' },
  statusCopy: { flex: 1, gap: 4 },
  statusTitle: { fontSize: 17, fontWeight: '600' },
  statusDescription: { fontSize: 13, lineHeight: 19 },
  metricsGrid: { flexDirection: 'row', gap: 12 },
  metric: { flex: 1, borderWidth: 1, borderRadius: 18, padding: 14, gap: 9 },
  metricIcon: { width: 32, height: 32, borderRadius: 10, alignItems: 'center', justifyContent: 'center' },
  metricLabel: { fontSize: 9, fontWeight: '700', letterSpacing: 1.1 },
  metricValue: { fontSize: 16, fontWeight: '600' },
  sectionHeading: { flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between', marginTop: 4 },
  sectionTitle: { fontSize: 20, fontWeight: '600' },
  sectionMeta: { fontSize: 12 },
  commandCard: { borderWidth: 1, borderRadius: 20, paddingHorizontal: 16 },
  commandRow: { minHeight: 55, flexDirection: 'row', alignItems: 'center', gap: 12 },
  command: { width: 70, fontSize: 13, fontWeight: '600' },
  commandDescription: { flex: 1, fontSize: 12 },
  refreshButton: { minHeight: 52, borderRadius: 17, alignItems: 'center', justifyContent: 'center', flexDirection: 'row', gap: 9 },
  refreshText: { fontSize: 14, fontWeight: '700' },
  controlCard: { borderWidth: 1, borderRadius: 20, padding: 16, gap: 14 },
  controlCopy: { gap: 4 },
  controlEyebrow: { fontSize: 9, fontWeight: '800', letterSpacing: 1.2 },
  controlTitle: { fontSize: 17, fontWeight: '700' },
  controlDescription: { fontSize: 12, lineHeight: 17 },
  controlButton: { minHeight: 44, borderRadius: 13, alignItems: 'center', justifyContent: 'center', flexDirection: 'row', gap: 8 },
  controlButtonText: { fontSize: 13, fontWeight: '800' },
  footerNote: { fontSize: 11, lineHeight: 17, textAlign: 'center', paddingHorizontal: 10 },
});