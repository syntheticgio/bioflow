/**
 * A model id: pick from the fetched list, or type one.
 *
 * Deliberately not a plain `<select>`. Some OpenAI-compatible servers implement
 * `/v1/models` poorly or not at all, OpenRouter returns hundreds of entries,
 * and a model id the user knows is valid must not be blocked by a listing
 * endpoint having a bad day. A datalist gives the dropdown when the list is
 * useful and gets out of the way when it is not.
 */
export function ModelCombo({
  value,
  options,
  onChange,
  id = "model-combo",
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  id?: string;
}) {
  return (
    <>
      <input
        className="settings-input"
        list={`${id}-options`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={options.length ? "Choose or type a model id" : "Type a model id"}
        spellCheck={false}
        autoComplete="off"
      />
      <datalist id={`${id}-options`}>
        {options.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      {options.length === 0 && (
        <p className="settings-hint">
          No models fetched yet — press Fetch models, or type an id directly.
        </p>
      )}
    </>
  );
}
