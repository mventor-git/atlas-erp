import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { Search } from "lucide-react";
import { useMemo } from "react";
import { z } from "zod";

import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";
import { count, money } from "@/lib/format";
import type { Catalog, CatalogItem } from "@/lib/schemas";

const ALL = "__all__";

/** A read-side filter, not a write: it narrows what the API already returned. */
const filterSchema = z.object({
  query: z.string().max(80, "Keep the search to 80 characters."),
  category: z.string(),
  inStock: z.boolean(),
});

type Filter = z.infer<typeof filterSchema>;

function matches(item: CatalogItem, filter: Filter): boolean {
  if (filter.category !== ALL && item.category !== filter.category) return false;
  if (filter.inStock && item.available <= 0) return false;
  const query = filter.query.trim().toLowerCase();
  if (!query) return true;
  return `${item.name} ${item.sku} ${item.brand}`.toLowerCase().includes(query);
}

function VariantTable({ item }: { item: CatalogItem }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Image</TableHead>
          <TableHead scope="col">Variant</TableHead>
          <TableHead scope="col">Colour</TableHead>
          <TableHead scope="col">Size</TableHead>
          <TableHead scope="col" className="text-right">
            On hand
          </TableHead>
          <TableHead scope="col" className="text-right">
            Reserved
          </TableHead>
          <TableHead scope="col" className="text-right">
            Available
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {item.variants.map((variant) => (
          <TableRow key={variant.variant_id}>
            <TableCell>
              <img
                src={variant.image_url}
                alt={variant.alt_text}
                loading="lazy"
                width={40}
                height={40}
                className="size-10 rounded-md border border-border object-cover"
              />
            </TableCell>
            <TableCell className="font-mono text-xs">{variant.variant_id}</TableCell>
            <TableCell>{variant.color}</TableCell>
            <TableCell>{variant.size}</TableCell>
            <TableCell className="text-right tabular-nums">{count(variant.on_hand)}</TableCell>
            <TableCell className="text-right tabular-nums">{count(variant.reserved)}</TableCell>
            <TableCell className="text-right tabular-nums">
              {variant.available > 0 ? (
                count(variant.available)
              ) : (
                <Badge variant="danger">Out of stock</Badge>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function CatalogPanel({ catalog }: { catalog: Catalog }) {
  const { register, watch, setValue, handleSubmit, formState } = useForm<Filter>({
    resolver: zodResolver(filterSchema),
    defaultValues: { query: "", category: ALL, inStock: false },
  });
  const filter = watch();
  const categories = useMemo(
    () => [...new Set(catalog.items.map((item) => item.category))].sort(),
    [catalog.items],
  );
  const visible = catalog.items.filter((item) => matches(item, filter));

  return (
    <section aria-labelledby="catalog-heading" className="grid gap-4">
      <div>
        <h2 id="catalog-heading" className="text-lg font-semibold">
          Catalogue
        </h2>
        <p className="text-sm text-muted-foreground">
          {count(catalog.items.length)} products from <code>{catalog.source}</code>, one
          remote Unsplash placeholder image per variant. The images are external links with no
          committed files, licences, or attribution.
        </p>
      </div>

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={handleSubmit(() => undefined)}
        aria-label="Filter catalogue"
      >
        <div className="grid gap-1.5">
          <Label htmlFor="catalog-query">Search</Label>
          <Input
            id="catalog-query"
            type="search"
            placeholder="Name, SKU, or brand"
            className="w-56"
            {...register("query")}
          />
        </div>
        <div className="grid gap-1.5">
          <Label id="catalog-category-label">Category</Label>
          <Select
            value={filter.category}
            onValueChange={(value) => setValue("category", value)}
          >
            <SelectTrigger aria-labelledby="catalog-category-label" className="w-48">
              <SelectValue placeholder="All categories" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All categories</SelectItem>
              {categories.map((category) => (
                <SelectItem key={category} value={category}>
                  {category}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-2 pb-1.5">
          <input
            id="catalog-in-stock"
            type="checkbox"
            className="size-4 accent-primary"
            {...register("inStock")}
          />
          <Label htmlFor="catalog-in-stock">In stock only</Label>
        </div>
        <Button type="button" variant="ghost" onClick={() => setValue("query", "")}>
          <Search aria-hidden="true" />
          Clear search
        </Button>
        {formState.errors.query ? (
          <p role="alert" className="w-full text-xs text-danger-text">
            {formState.errors.query.message}
          </p>
        ) : null}
      </form>

      {visible.length === 0 ? (
        <p className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
          No product matches this filter. Clear it to see all {count(catalog.items.length)}{" "}
          products.
        </p>
      ) : (
        <ul className="grid list-none gap-4 p-0 lg:grid-cols-2 xl:grid-cols-3">
          {visible.map((item) => (
            <li key={item.item_id}>
              <Card>
                <img
                  src={item.variants[0]?.image_url}
                  alt={item.variants[0]?.alt_text ?? item.name}
                  loading="lazy"
                  className="aspect-[4/3] w-full rounded-t-lg object-cover"
                />
                <CardHeader>
                  <CardTitle>
                    <h3>{item.name}</h3>
                  </CardTitle>
                  <CardDescription>
                    {item.brand} &middot; {item.category} &middot;{" "}
                    {count(item.variants.length)} variants
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-3">
                  <p className="tabular-nums">
                    <strong>{money(item.price_cents)}</strong>{" "}
                    <span className="text-muted-foreground">
                      per unit &middot; <code>{item.sku}</code>
                    </span>
                  </p>
                  <p className="flex flex-wrap gap-1.5">
                    {item.variants.map((variant) => (
                      <Badge key={variant.variant_id} variant="info">
                        {variant.color} / {variant.size}
                      </Badge>
                    ))}
                  </p>
                  <VariantTable item={item} />
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
