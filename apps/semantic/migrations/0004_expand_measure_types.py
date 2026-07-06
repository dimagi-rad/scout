from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("semantic", "0003_thread_bound_canvas_changeset"),
    ]

    operations = [
        migrations.AlterField(
            model_name="semanticfield",
            name="measure_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("count", "Count"),
                    ("count_distinct", "Count distinct"),
                    ("count_distinct_approx", "Approximate count distinct"),
                    ("sum", "Sum"),
                    ("avg", "Average"),
                    ("min", "Minimum"),
                    ("max", "Maximum"),
                    ("number", "Number"),
                ],
                max_length=32,
            ),
        ),
    ]
